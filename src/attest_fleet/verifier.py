"""Step 5 of the loop: result verification.

The verifier does not ask the agent whether it succeeded. It reads the system of
record and checks the post-conditions the task implies. Only tasks with no
deterministic post-condition fall back to an LLM auditor, and those are marked."""

from __future__ import annotations

from typing import Optional

from .domain import Check, Claim, Task, Verification
from .store import BaseStore


def _refund_checks(store: BaseStore, task: Task) -> list[Check]:
    checks: list[Check] = []
    if not task.order_id:
        return [Check(name="order_id_present", passed=False, detail="task has no order_id")]
    order = store.get("orders", task.order_id)
    if order is None:
        return [Check(name="order_exists", passed=False, detail=f"no order {task.order_id}")]
    refunds = [r for r in store.query("refunds", order_id=task.order_id) if r.get("state") == "completed"]
    completed = round(sum(float(r["amount"]) for r in refunds), 2)
    expected = float(task.amount) if task.amount is not None else float(order["total"]) - 0.0
    checks.append(Check(name="refund_completed_amount", passed=abs(completed - expected) < 0.01,
                        detail=f"completed refunds {completed:.2f}, expected {expected:.2f}"))
    checks.append(Check(name="order_refunded_field", passed=abs(float(order.get("refunded", 0)) - expected) < 0.01,
                        detail=f"order.refunded={order.get('refunded', 0)}"))
    pending = [r for r in store.query("refunds", order_id=task.order_id) if r.get("state") != "completed"]
    if pending:
        checks.append(Check(name="no_pending_refunds", passed=False, detail=f"{len(pending)} refund(s) stuck in {pending[0].get('state')}"))
    return checks


def _address_checks(store: BaseStore, task: Task) -> list[Check]:
    if not task.customer_id or not task.new_address:
        return [Check(name="params_present", passed=False, detail="customer_id and new_address required")]
    c = store.get("customers", task.customer_id)
    if c is None:
        return [Check(name="customer_exists", passed=False, detail=f"no customer {task.customer_id}")]
    want = " ".join(task.new_address.split()).lower()
    have = " ".join(str(c.get("address", "")).split()).lower()
    return [Check(name="address_matches", passed=want == have, detail=f"address is '{c.get('address')}'")]


def _cancel_checks(store: BaseStore, task: Task) -> list[Check]:
    sub_id = task.subscription_id
    if not sub_id and task.customer_id:
        subs = store.query("subscriptions", customer_id=task.customer_id)
        sub_id = subs[0]["id"] if len(subs) == 1 else None
    if not sub_id:
        return [Check(name="subscription_resolved", passed=False, detail="could not resolve a single subscription")]
    s = store.get("subscriptions", sub_id)
    if s is None:
        return [Check(name="subscription_exists", passed=False, detail=f"no subscription {sub_id}")]
    return [Check(name="status_cancelled", passed=s.get("status") == "cancelled", detail=f"status is '{s.get('status')}'")]


def _unlock_checks(store: BaseStore, task: Task) -> list[Check]:
    if not task.customer_id:
        return [Check(name="customer_id_present", passed=False, detail="task has no customer_id")]
    c = store.get("customers", task.customer_id)
    if c is None:
        return [Check(name="customer_exists", passed=False, detail=f"no customer {task.customer_id}")]
    return [Check(name="account_unlocked", passed=c.get("locked") is False, detail=f"locked={c.get('locked')}")]


def _delete_checks(store: BaseStore, task: Task) -> list[Check]:
    """Account deletion is the only irreversible action in the fleet, and it was the only
    one with no deterministic check — so it fell through to the LLM auditor, the very thing
    this project cites research against. Read the record instead."""
    cid = task.customer_id or ""
    cust = store.get("customers", cid)
    gone = cust is None or bool(cust.get("deleted"))
    checks = [Check(name="customer_deleted", passed=gone,
                    detail="customer record absent" if cust is None
                    else f"customer.deleted={cust.get('deleted', False)}")]
    active = [s for s in store.query("subscriptions", customer_id=cid) if s.get("status") == "active"]
    checks.append(Check(name="no_active_subscriptions", passed=not active,
                        detail=f"{len(active)} active subscription(s) remain"))
    return checks


POSTCONDITIONS = {
    "refund": _refund_checks,
    "delete_account": _delete_checks,
    "address_change": _address_checks,
    "cancel_subscription": _cancel_checks,
    "unlock_account": _unlock_checks,
}


def verify(store: BaseStore, task: Task, claim: Optional[Claim],
           run_id: str = "") -> Verification:
    from .process import check_process, summarise
    from .tools import MUTATING

    proc = check_process(store, run_id, task, claim, MUTATING) if run_id else []
    proc_verdict = summarise(proc)["conformant"]

    fn = POSTCONDITIONS.get(task.type)
    if fn is None:
        return Verification(task_id=task.id, verified=None, method="none",
                            process_checks=proc, process_conformant=proc_verdict,
                            detail="no deterministic post-condition; auditor required")
    checks = fn(store, task)
    verified = all(c.passed for c in checks)
    claimed_done = bool(claim and claim.outcome == "done")
    return Verification(
        task_id=task.id,
        verified=verified,
        method="postcondition",
        checks=checks,
        process_checks=proc,
        process_conformant=proc_verdict,
        silent_failure=claimed_done and not verified,
        false_alarm=(claim is not None and not claimed_done and verified),
        detail="; ".join(f"{c.name}={'ok' if c.passed else 'FAIL'} ({c.detail})" for c in checks),
    )
