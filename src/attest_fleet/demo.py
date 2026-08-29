"""Synthetic sample runs for local UI work and screenshots — NO LLM calls.

Gated behind ATTEST_DEMO=1 and only ever loaded into a MemoryStore, so it can never
pollute a real Firestore. Every run here is fabricated; production shows real runs only."""

from __future__ import annotations

from .domain import Approval, Check, Claim, Event, RunRecord, Task, TaskResult, Verification
from .store import BaseStore

_TS = "2026-08-25T20:"


def _run(store: BaseStore, rid, subject, ref, status, task, claim, verif, events, minute, gt=None):
    rec = RunRecord(
        id=rid, ticket={"id": "tkt_" + rid, "source": "simulator", "customer_ref": ref, "subject": subject,
                        "body": f"{subject} — request from {ref}.", "created_at": f"{_TS}{minute}:00Z"},
        plan={"tasks": [task.model_dump()], "customer_resolution": claim.get("resolution", "Resolved a single matching customer."), "summary": subject},
        results=[TaskResult(task=task, claim=Claim(**{k: v for k, v in claim.items() if k != "resolution"}), verification=verif).model_dump()],
        status=status, started_at=f"{_TS}{minute}:00Z", finished_at=f"{_TS}{minute}:09Z", ground_truth=gt,
    )
    store.set("runs", rid, rec.model_dump() if hasattr(rec, "model_dump") else rec)
    for i, e in enumerate(events):
        ev = Event(run_id=rid, task_id=task.id, agent=e.get("agent"), kind=e["kind"], name=e["name"],
                   args_json=e.get("args", ""), result_json=e.get("result", ""), latency_ms=e.get("ms"),
                   ts=f"{_TS}{minute}:0{i}Z")
        store.set("events", ev.id, ev.model_dump())


def seed_demo(store: BaseStore, force: bool = False) -> None:
    """Load the fabricated sample runs. MEMORY STORES ONLY, with no override.

    These runs were never executed by an agent. A deployed board is something a judge or
    an operator may reasonably read as live results, so fabricated data must not be able
    to reach one, not even behind a flag. The deployed board is populated from the real
    eval evidence instead (see web._load_eval_evidence)."""
    if store.backend != "memory":
        return
    if not force and store.get("runs", "run_demo1"):
        return

    # 1 — verified refund
    _run(store, "run_demo1", "Refund last month", "Priya Sharma", "verified",
         Task(id="t1", type="refund", worker="billing_agent", customer_id="cus_1001", order_id="ord_5001", amount=49.0, instruction="Refund 49 on ord_5001.", rationale="Customer asked for the Pro monthly refund."),
         {"task_id": "t1", "outcome": "done", "confidence": 0.98, "evidence": "issue_refund returned state=completed; order.refunded=49.0, status=refunded.", "resolution": "Two customers named Priya Sharma; disambiguated to cus_1001 by the email in the ticket body."},
         Verification(task_id="t1", verified=True, method="postcondition", checks=[Check(name="refund_completed_amount", passed=True, detail="completed refunds 49.00, expected 49.00"), Check(name="order_refunded_field", passed=True, detail="order.refunded=49.0")], detail="all post-conditions hold"),
         [{"kind": "tool", "name": "find_customer", "agent": "fleet_controller", "result": '{"count":2,"ambiguous":true}', "ms": 41},
          {"kind": "tool", "name": "issue_refund", "agent": "billing_agent", "args": '{"order_id":"ord_5001","amount":49}', "result": '{"state":"completed"}', "ms": 88},
          {"kind": "verify", "name": "verified", "result": '{"verified":true}'}], 12, gt=True)

    # 2 — silent failure (address draft) + playbook lesson
    _run(store, "run_demo2", "Change my address", "cus_1004", "silent_failure",
         Task(id="t1", type="address_change", worker="account_agent", customer_id="cus_1004", new_address="200 Century Ave, Pudong, Shanghai 200120", instruction="Update address for cus_1004.", rationale="Customer moved."),
         {"task_id": "t1", "outcome": "done", "confidence": 0.83, "evidence": "update_address returned success: 'address saved'."},
         Verification(task_id="t1", verified=False, method="postcondition", silent_failure=True, checks=[Check(name="address_matches", passed=False, detail="address is '88 Nanjing Rd, Shanghai' (write went to a draft field)")], detail="claimed done; system of record disagrees"),
         [{"kind": "tool", "name": "update_address", "agent": "account_agent", "args": '{"customer_id":"cus_1004"}', "result": '{"status":"success","message":"address saved"}', "ms": 62},
          {"kind": "verify", "name": "verified", "result": '{"verified":false,"silent_failure":true}'},
          {"kind": "experience", "name": "lesson_captured", "agent": "account_agent", "args": '{"signature":"address_draft"}'}], 20, gt=False)

    # 3 — pending approval (large refund)
    _run(store, "run_demo3", "Refund annual plan", "cus_1003", "pending_approval",
         Task(id="t1", type="refund", worker="billing_agent", customer_id="cus_1003", order_id="ord_5003", amount=490.0, instruction="Refund 490 on ord_5003.", rationale="Customer wants the annual plan refunded."),
         {"task_id": "t1", "outcome": "blocked", "confidence": 0.4, "evidence": "issue_refund returned pending_approval (apr_demo); the money did not move.", "note": "awaiting approval apr_demo"},
         Verification(task_id="t1", verified=False, method="postcondition", checks=[Check(name="refund_completed_amount", passed=False, detail="completed refunds 0.00, expected 490.00")], detail="action gated before execution"),
         [{"kind": "policy", "name": "approval_required", "agent": "billing_agent", "args": '{"tool":"issue_refund","amount":490}', "result": '{"approval_id":"apr_demo"}'}], 31)
    store.set("approvals", "apr_demo", Approval(id="apr_demo", run_id="run_demo3", ticket_id="tkt_run_demo3", task_id="t1", agent="billing_agent", action="issue_refund", args_json='{"order_id": "ord_5003", "amount": 490, "reason": "Full annual refund"}', risk_reason="refund 490.00 exceeds the 100 auto-approval limit", created_at=f"{_TS}31:02Z").model_dump())

    # 4 — verified cancel
    _run(store, "run_demo4", "Cancel my subscription", "d.okafor@example.com", "verified",
         Task(id="t1", type="cancel_subscription", worker="account_agent", customer_id="cus_1003", subscription_id="sub_9003", instruction="Cancel sub_9003.", rationale="Budget."),
         {"task_id": "t1", "outcome": "done", "confidence": 0.95, "evidence": "cancel_subscription returned status=cancelled; read-back confirms."},
         Verification(task_id="t1", verified=True, method="postcondition", checks=[Check(name="status_cancelled", passed=True, detail="status is 'cancelled'")], detail="post-condition holds"),
         [{"kind": "tool", "name": "cancel_subscription", "agent": "account_agent", "result": '{"state":"cancelled"}', "ms": 71}, {"kind": "verify", "name": "verified", "result": '{"verified":true}'}], 38, gt=True)

    # 5 — failed unlock (loud error)
    _run(store, "run_demo5", "Locked out", "cus_1005", "failed",
         Task(id="t1", type="unlock_account", worker="account_agent", customer_id="cus_1005", instruction="Unlock cus_1005.", rationale="Failed sign-ins."),
         {"task_id": "t1", "outcome": "failed", "confidence": 0.2, "evidence": "unlock_account returned IAM_TIMEOUT; account still locked."},
         Verification(task_id="t1", verified=False, method="postcondition", checks=[Check(name="account_unlocked", passed=False, detail="locked=true")], detail="tool errored"),
         [{"kind": "tool", "name": "unlock_account", "agent": "account_agent", "result": '{"status":"error","error":"IAM_TIMEOUT"}', "ms": 30}, {"kind": "verify", "name": "verified", "result": '{"verified":false}'}], 44, gt=True)

    # 6 — verified partial refund
    _run(store, "run_demo6", "Partial refund", "meiling@example.com", "verified",
         Task(id="t1", type="refund", worker="billing_agent", customer_id="cus_1004", order_id="ord_5004", amount=80.0, instruction="Refund 80 on ord_5004.", rationale="Removed seats."),
         {"task_id": "t1", "outcome": "done", "confidence": 0.97, "evidence": "issue_refund completed 80.00; order.refunded=80.0."},
         Verification(task_id="t1", verified=True, method="postcondition", checks=[Check(name="refund_completed_amount", passed=True, detail="completed refunds 80.00, expected 80.00")], detail="ok"),
         [{"kind": "tool", "name": "issue_refund", "agent": "billing_agent", "result": '{"state":"completed"}', "ms": 79}, {"kind": "verify", "name": "verified", "result": '{"verified":true}'}], 51, gt=True)

    # 7 — vision intake: customer attached a screenshot; the fleet reads it, then refunds
    _run(store, "run_demo7", "Charged twice, see screenshot", "cus_1006", "verified",
         Task(id="t1", type="refund", worker="billing_agent", customer_id="cus_1006", order_id="ord_5006", amount=49.0, instruction="Refund the duplicate 49 charge on ord_5006.", rationale="Screenshot shows a duplicate charge."),
         {"task_id": "t1", "outcome": "done", "confidence": 0.96, "evidence": "issue_refund completed 49.00 on ord_5006; order.refunded=49.0.", "resolution": "Customer cus_1006; the attached screenshot named order ord_5006 and a duplicate 49.00 charge."},
         Verification(task_id="t1", verified=True, method="postcondition", checks=[Check(name="refund_completed_amount", passed=True, detail="completed refunds 49.00, expected 49.00")], detail="ok"),
         [{"kind": "model", "name": "vision_read", "agent": "vision_reader", "args": '{"saw":"Screenshot of a billing history showing two identical charges of $49.00 for order ord_5006 dated the same day."}', "ms": 640},
          {"kind": "tool", "name": "issue_refund", "agent": "billing_agent", "result": '{"state":"completed"}', "ms": 83},
          {"kind": "verify", "name": "verified", "result": '{"verified":true}'}], 57, gt=True)
    r7=store.get("runs","run_demo7"); r7["vision"]="Screenshot of a billing history showing two identical charges of $49.00 for order ord_5006 dated the same day."; store.set("runs","run_demo7",r7)

    store.set("playbook", "address_draft", {"worker": "account_agent", "task_type": "address_change",
              "lesson": "After update_address, call get_customer and confirm the 'address' field equals the requested address before claiming done. A success message alone is not evidence.",
              "count": 1, "first_seen": f"{_TS}20:00Z", "last_seen": f"{_TS}20:00Z", "last_run": "run_demo2"})
