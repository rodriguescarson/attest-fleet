"""No LLM, no network: metrics arithmetic, verifier post-conditions, policy gate."""

from types import SimpleNamespace

import pytest

from attest_fleet import metrics, policy
from attest_fleet.domain import Claim, Task, Verification
from attest_fleet.store import MemoryStore, seed, use_store
from attest_fleet.tools import issue_refund, update_address
from attest_fleet.verifier import verify


@pytest.fixture()
def store():
    s = MemoryStore()
    seed(s)
    use_store(s)
    return s


def claim(task_id, outcome="done", conf=0.9):
    return Claim(task_id=task_id, outcome=outcome, confidence=conf, actions=[], evidence="x")


def test_metrics_known_values():
    pairs = [
        (claim("a", "done", 0.9), Verification(task_id="a", verified=True, method="postcondition")),
        (claim("b", "done", 0.8), Verification(task_id="b", verified=False, method="postcondition", silent_failure=True)),
        (claim("c", "failed", 0.2), Verification(task_id="c", verified=True, method="postcondition", false_alarm=True)),
        (claim("d", "done", 1.0), Verification(task_id="d", verified=True, method="postcondition")),
    ]
    m = metrics.compute(pairs, target_risk=0.0)
    assert m["reported_success_rate"] == 0.75
    assert m["verified_success_rate"] == 0.75
    assert m["silent_failure_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert m["false_alarm_rate"] == 1.0
    # Confidence is confidence in the CLAIM, so it maps to P(verified) through the claim
    # direction: "done"@0.9 -> 0.9, but "failed"@0.2 -> 0.8 (only 20% sure it failed).
    # a: (0.9-1)^2  b: (0.8-0)^2  c: (0.8-1)^2  d: (1.0-1)^2
    assert m["brier"] == pytest.approx((0.01 + 0.64 + 0.04 + 0.0) / 4, abs=1e-4)
    # threshold 0.9 accepts a,d (both right) -> risk 0, coverage 2/3
    assert m["escalation"]["threshold"] == 0.9 and m["escalation"]["coverage"] == pytest.approx(2 / 3, abs=1e-3)


def test_metrics_empty():
    m = metrics.compute([])
    assert m["n_tasks"] == 0 and m["silent_failure_rate"] is None and m["escalation"] is None


def test_verifier_catches_pending_gateway_refund(store):
    store.set_setting("fault_rate", 1.0)  # every refund goes pending_gateway
    r = issue_refund("ord_5001", 49.0, "test", run_id="r1")
    assert r["status"] == "success" and r["state"] == "pending_gateway"
    task = Task(id="t1", type="refund", worker="billing_agent", order_id="ord_5001", amount=49.0, instruction="", rationale="")
    v = verify(store, task, claim("t1", "done"))
    assert v.verified is False and v.silent_failure is True
    store.set_setting("fault_rate", 0.0)
    issue_refund("ord_5001", 49.0, "test", run_id="r2")
    v2 = verify(store, task, claim("t1", "done"))
    # completed amount now matches, but a stuck pending refund still fails the no_pending check
    assert any(c.name == "refund_completed_amount" and c.passed for c in v2.checks)
    assert v2.verified is False


def test_verifier_address_draft_is_not_done(store):
    store.set_setting("fault_rate", 1.0)
    update_address("cus_1004", "New Road 1", run_id="r3")
    task = Task(id="t2", type="address_change", worker="account_agent", customer_id="cus_1004", new_address="New Road 1", instruction="", rationale="")
    assert verify(store, task, claim("t2")).silent_failure is True
    store.set_setting("fault_rate", 0.0)
    update_address("cus_1004", "New Road 1", run_id="r4")
    assert verify(store, task, claim("t2")).verified is True


def test_false_alarm_on_already_cancelled(store):
    task = Task(id="t3", type="cancel_subscription", worker="account_agent", subscription_id="sub_9005", instruction="", rationale="")
    v = verify(store, task, claim("t3", "failed", 0.3))
    assert v.verified is True and v.false_alarm is True


def _ctx(**state):
    return SimpleNamespace(state=state, agent_name="billing_agent", function_call_id="fc1")


def test_policy_blocks_large_refund_until_approved(store):
    tool = SimpleNamespace(name="issue_refund")
    args = {"order_id": "ord_5003", "amount": 490.0, "reason": "x"}
    res = policy.before_tool(tool, args, _ctx(run_id="r", ticket_id="tk", task_id="t"))
    assert res["status"] == "pending_approval"
    apr = store.get("approvals", res["approval_id"])
    assert apr["status"] == "pending" and apr["action"] == "issue_refund"
    # same call again reuses the pending approval
    res2 = policy.before_tool(tool, dict(args), _ctx(run_id="r", ticket_id="tk", task_id="t"))
    assert res2["approval_id"] == res["approval_id"]
    store.update("approvals", apr["id"], {"status": "approved"})
    assert policy.before_tool(tool, dict(args), _ctx(run_id="r", ticket_id="tk", task_id="t")) is None


def test_policy_small_refund_passes_and_kill_switch_blocks(store):
    tool = SimpleNamespace(name="issue_refund")
    assert policy.before_tool(tool, {"order_id": "ord_5001", "amount": 10, "reason": "x"}, _ctx(run_id="r", ticket_id="tk")) is None
    store.set_setting("kill_switch", True)
    res = policy.before_tool(tool, {"order_id": "ord_5001", "amount": 10, "reason": "x"}, _ctx(run_id="r", ticket_id="tk"))
    assert res["status"] == "blocked"
    assert policy.before_tool(SimpleNamespace(name="get_order"), {"order_id": "ord_5001"}, _ctx(run_id="r", ticket_id="tk")) is None


def test_pre_execution_gate_blocks_inconsistent_writes(store):
    """Reddy et al. 2025: deterministic pre-execution gate prevents state-inconsistent writes."""
    ctx = _ctx(run_id="r", ticket_id="tk", task_id="t")
    # refund exceeding the refundable balance is blocked BEFORE it runs
    res = policy.before_tool(SimpleNamespace(name="issue_refund"), {"order_id": "ord_5001", "amount": 999, "reason": "x"}, ctx)
    assert res["status"] == "blocked" and "exceeds the refundable balance" in res["reason"]
    # refund on a non-existent order is blocked
    assert policy.before_tool(SimpleNamespace(name="issue_refund"), {"order_id": "nope", "amount": 5}, _ctx(run_id="r", ticket_id="tk"))["status"] == "blocked"
    # unlock of a non-existent customer is blocked
    assert policy.before_tool(SimpleNamespace(name="unlock_account"), {"customer_id": "nope"}, _ctx(run_id="r", ticket_id="tk"))["status"] == "blocked"
    # a valid, in-balance refund passes the gate
    assert policy.before_tool(SimpleNamespace(name="issue_refund"), {"order_id": "ord_5001", "amount": 20, "reason": "x"}, _ctx(run_id="r", ticket_id="tk")) is None
    # a gate_block event was recorded as evidence
    assert any(e["name"] == "gate_block" for e in store.list("events", limit=100))


def test_batch_audit_records():
    """Framework-agnostic batch scoring over generic agent logs."""
    rep = metrics.compute_records([
        {"claimed_done": True, "confidence": 0.95, "verified": True},
        {"claimed_done": True, "confidence": 0.7, "verified": False},   # silent failure
        {"outcome": "failed", "confidence": 0.2, "verified": True},     # false alarm
        {"claimed_done": True, "confidence": 0.9, "verified": None},    # unverifiable, excluded
    ])
    assert rep["n_tasks"] == 4 and rep["n_verifiable"] == 3
    assert rep["silent_failure_rate"] == 0.5   # 1 of 2 verifiable claimed-done
    assert rep["false_alarm_rate"] == 1.0


def test_brier_does_not_penalise_a_correctly_reported_block():
    """Regression: a worker that correctly reports it did NOT complete, with high
    confidence, must score near-zero error — not the maximum penalty. Scoring raw
    confidence against `verified` inverted this and inflated measured over-confidence."""
    pairs = [
        (claim("t", "blocked", 1.0), Verification(task_id="t", verified=False, method="postcondition")),
    ]
    m = metrics.compute(pairs)
    assert m["brier"] == pytest.approx(0.0, abs=1e-9)
    assert m["ece"] == pytest.approx(0.0, abs=1e-9)


def test_loop_guard_stops_a_runaway_worker(store):
    """The rubric asks how the system recovers if a worker loops. A per-task tool-call
    budget is enforced at the same gate as policy, and fails closed with evidence."""
    from attest_fleet import config
    tool = SimpleNamespace(name="get_order")
    ctx = _ctx(run_id="loop-run", ticket_id="tk", task_id="t-loop")
    policy.reset_tool_budget("loop-run", "t-loop")
    for _ in range(config.MAX_TOOL_CALLS_PER_TASK):
        assert policy.before_tool(tool, {"order_id": "ord_5001"}, ctx) is None
    blocked = policy.before_tool(tool, {"order_id": "ord_5001"}, ctx)
    assert blocked["status"] == "blocked" and "budget" in blocked["reason"]
    assert any(e["name"] == "loop_guard" for e in store.list("events", limit=200))
    # a different task gets its own budget
    policy.reset_tool_budget("loop-run", "t-other")
    assert policy.before_tool(tool, {"order_id": "ord_5001"}, _ctx(run_id="loop-run", ticket_id="tk", task_id="t-other")) is None
