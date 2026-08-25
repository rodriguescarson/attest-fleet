"""Step 8: experience capture.

Every verified failure is distilled into a lesson keyed by its signature, stored in
the playbook collection, and injected into the responsible worker's instruction on
its next run. The fleet gets better without anyone editing a prompt."""

from __future__ import annotations

import json
from typing import Optional

from . import config
from .domain import Claim, Event, Task, Verification, now_iso
from .store import BaseStore

# signature -> (worker, lesson)
LESSONS: dict[str, tuple[str, str]] = {
    "refund_pending_gateway": ("billing_agent", "A refund whose result has state 'pending_gateway' has NOT completed. Only state 'completed' counts. Report outcome='failed' or 'blocked', never 'done', and quote the state."),
    "address_draft": ("account_agent", "After update_address, call get_customer and confirm the 'address' field equals the requested address before claiming done. A success message alone is not evidence."),
    "cancel_requested": ("account_agent", "cancel_subscription is only effective when the subscription status is 'cancelled'. 'cancel_requested' or 'cancellation queued' means it did not happen yet."),
    "ambiguous_customer": ("account_agent", "When find_customer returns more than one match, never pick one by position. Disambiguate with email, order id or city from the ticket, or report failed with the candidates listed."),
    "approval_claimed_done": ("billing_agent", "If a tool returns status 'pending_approval' the action did NOT run. Report outcome='blocked' with the approval id."),
    "unlock_error_claimed_done": ("account_agent", "If unlock_account returns an error, the account is still locked. Do not report done on an error result."),
}


def signature(task: Task, claim: Optional[Claim], verification: Verification, events: list[dict]) -> Optional[str]:
    if not verification.silent_failure:
        return None
    results = [e.get("result_json", "") for e in events if e.get("kind") == "tool"]
    blob = " ".join(results)
    if task.type == "refund" and "pending_gateway" in blob:
        return "refund_pending_gateway"
    if "pending_approval" in blob:
        return "approval_claimed_done"
    if task.type == "address_change" and "address saved" in blob:
        return "address_draft"
    if task.type == "cancel_subscription" and "cancellation queued" in blob:
        return "cancel_requested"
    if task.type == "unlock_account" and "IAM_TIMEOUT" in blob:
        return "unlock_error_claimed_done"
    if '"ambiguous": true' in blob.lower() or '"ambiguous":true' in blob.lower():
        return "ambiguous_customer"
    return None


def capture(store: BaseStore, run_id: str, task: Task, claim: Optional[Claim], verification: Verification) -> Optional[str]:
    events = [e for e in store.query("events", run_id=run_id) if e.get("task_id") == task.id]
    sig = signature(task, claim, verification, events)
    if sig is None:
        return None
    worker, lesson = LESSONS[sig]
    doc = store.get("playbook", sig)
    if doc:
        store.update("playbook", sig, {"count": int(doc.get("count", 0)) + 1, "last_seen": now_iso(), "last_run": run_id})
    else:
        store.set("playbook", sig, {"worker": worker, "task_type": task.type, "lesson": lesson, "count": 1, "first_seen": now_iso(), "last_seen": now_iso(), "last_run": run_id})
    ev = Event(run_id=run_id, task_id=task.id, agent=worker, kind="experience", name="lesson_captured", args_json=json.dumps({"signature": sig}))
    store.set("events", ev.id, ev.model_dump())
    return sig


def lessons_for(store: BaseStore, worker: str, limit: int = config.PLAYBOOK_LESSONS) -> list[str]:
    rows = [r for r in store.list("playbook", limit=100) if r.get("worker") == worker]
    rows.sort(key=lambda r: (-int(r.get("count", 0)), r.get("last_seen", "")))
    return [r["lesson"] for r in rows[:limit]]
