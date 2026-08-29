"""Process conformance: the route to the outcome, checked separately from the outcome."""

from types import SimpleNamespace

import pytest

from attest_fleet import policy, process
from attest_fleet.domain import Claim, Task
from attest_fleet.store import MemoryStore, seed, use_store
from attest_fleet.tools import MUTATING, issue_refund, update_address
from attest_fleet.verifier import verify


@pytest.fixture()
def store():
    s = MemoryStore(); seed(s); use_store(s); return s


def _ctx(**st):
    return SimpleNamespace(state=st, agent_name="billing_agent", function_call_id="fc")


def claim(tid, outcome="done", conf=0.9):
    return Claim(task_id=tid, outcome=outcome, confidence=conf, actions=[], evidence="x")


def test_claiming_done_without_reading_back_is_a_process_violation(store):
    """A worker that mutated and claimed done without re-reading is reporting its intent."""
    tool = SimpleNamespace(name="issue_refund")
    ctx = _ctx(run_id="r", ticket_id="tk", task_id="t")
    policy.reset_tool_budget("r", "t")
    args = {"order_id": "ord_5001", "amount": 49.0, "reason": "x"}
    policy.before_tool(tool, dict(args), ctx)
    policy.after_tool(tool, dict(args), ctx, issue_refund(**dict(args, run_id="r")))

    task = Task(id="t", type="refund", worker="billing_agent", order_id="ord_5001",
                amount=49.0, instruction="", rationale="")
    checks = process.check_process(store, "r", task, claim("t"), MUTATING)
    rb = next(c for c in checks if c.name == "read_back_after_write")
    assert rb.passed is False and "without re-reading" in rb.detail


def test_reading_back_after_the_write_conforms(store):
    tool = SimpleNamespace(name="issue_refund")
    read = SimpleNamespace(name="get_order")
    ctx = _ctx(run_id="r", ticket_id="tk", task_id="t")
    policy.reset_tool_budget("r", "t")
    args = {"order_id": "ord_5001", "amount": 49.0, "reason": "x"}
    policy.before_tool(tool, dict(args), ctx)
    policy.after_tool(tool, dict(args), ctx, issue_refund(**dict(args, run_id="r")))
    policy.before_tool(read, {"order_id": "ord_5001"}, ctx)
    policy.after_tool(read, {"order_id": "ord_5001"}, ctx, {"status": "success"})

    task = Task(id="t", type="refund", worker="billing_agent", order_id="ord_5001",
                amount=49.0, instruction="", rationale="")
    checks = process.check_process(store, "r", task, claim("t"), MUTATING)
    assert next(c for c in checks if c.name == "read_back_after_write").passed is True


def test_acting_on_the_wrong_entity_is_caught_even_when_the_end_state_passes(store):
    """The two reported verifier blind spots were exactly this: the action happened, on the
    wrong person, and the end-state check passed because it only asked whether it happened."""
    tool = SimpleNamespace(name="update_address")
    ctx = _ctx(run_id="r", ticket_id="tk", task_id="t")
    policy.reset_tool_budget("r", "t")
    wrong = {"customer_id": "cus_1002", "new_address": "9 Elsewhere Rd"}
    policy.before_tool(tool, dict(wrong), ctx)
    policy.after_tool(tool, dict(wrong), ctx, update_address(**dict(wrong, run_id="r")))

    task = Task(id="t", type="address_change", worker="account_agent",
                customer_id="cus_1004", new_address="9 Elsewhere Rd",
                instruction="", rationale="")
    checks = process.check_process(store, "r", task, claim("t"), MUTATING)
    ent = next(c for c in checks if c.name == "acted_on_the_named_entity")
    assert ent.passed is False and "cus_1002" in ent.detail


def test_conformance_is_reported_separately_from_the_end_state(store):
    """An end-state pass with a process failure is not a silent failure, and merging the
    two verdicts would lose the distinction the field exists to make."""
    tool = SimpleNamespace(name="update_address")
    ctx = _ctx(run_id="r", ticket_id="tk", task_id="t")
    policy.reset_tool_budget("r", "t")
    args = {"customer_id": "cus_1004", "new_address": "New Road 1"}
    policy.before_tool(tool, dict(args), ctx)
    policy.after_tool(tool, dict(args), ctx, update_address(**dict(args, run_id="r")))

    task = Task(id="t", type="address_change", worker="account_agent",
                customer_id="cus_1004", new_address="New Road 1", instruction="", rationale="")
    v = verify(store, task, claim("t"), run_id="r")
    assert v.verified is True                 # the end state is right
    assert v.process_conformant is False      # but it never read back
    assert v.silent_failure is False          # and that is NOT a silent failure


def test_no_mutating_step_means_no_conformance_verdict():
    assert process.summarise([])["conformant"] is None
