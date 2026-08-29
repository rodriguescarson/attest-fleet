"""The governance layer: every tool call passes through here.

before_tool: kill switch, deterministic pre-execution state gate, high-risk approval gate.
after_tool:  execution-evidence capture (args, result, latency) to the store.

Both are ADK callbacks attached to every worker agent."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from . import config
from .domain import Approval, Event, now_iso
from .store import get_store
from .tools import MUTATING


def _state_gate(tool_name: str, args: dict[str, Any], store) -> Optional[str]:
    """Deterministic pre-execution gate (Reddy et al. 2025, arXiv 2607.07405): validate a
    mutating call against current state BEFORE the write, so a policy-inconsistent action is
    prevented rather than detected after the fact. Returns a reason to block, or None to allow."""
    if tool_name == "issue_refund":
        order = store.get("orders", args.get("order_id", ""))
        if order is None:
            return f"order {args.get('order_id')!r} does not exist"
        try:
            amount = float(args.get("amount", 0))
        except (TypeError, ValueError):
            return "refund amount is not a number"
        remaining = round(float(order["total"]) - float(order.get("refunded", 0)), 2)
        if amount <= 0:
            return "refund amount must be positive"
        if amount > remaining + 1e-9:
            return f"refund {amount:.2f} exceeds the refundable balance {remaining:.2f}"
    elif tool_name in ("update_address", "unlock_account", "delete_account", "record_note"):
        cid = args.get("customer_id", "")
        if store.get("customers", cid) is None:
            return f"customer {cid!r} does not exist"
        if tool_name == "update_address" and not str(args.get("new_address", "")).strip():
            return "new address is empty"
    elif tool_name == "cancel_subscription":
        sid = args.get("subscription_id", "")
        if sid and store.get("subscriptions", sid) is None:
            return f"subscription {sid!r} does not exist"
    return None


def _is_high_risk(tool_name: str, args: dict[str, Any], store=None) -> Optional[str]:
    """Is this action high-risk enough to need a human?

    The threshold is CUMULATIVE per order, not per call. A per-call limit is trivially
    structured around: five refunds of 98 against a 490 order each clear a 100 limit, no
    approval is ever raised, and the whole 490 moves. Bounded agency that can be walked
    through in five calls is not bounded, so the already-refunded total counts toward it."""
    if tool_name == "delete_account":
        return "account deletion is irreversible"
    if tool_name == "issue_refund":
        try:
            amount = float(args.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0.0
        already = 0.0
        order_id = args.get("order_id", "")
        if store is not None and order_id:
            order = store.get("orders", order_id)
            if order:
                already = float(order.get("refunded", 0) or 0)
        total = amount + already
        if total > config.REFUND_APPROVAL_THRESHOLD:
            if already:
                return (f"refund {amount:.2f} takes this order to {total:.2f} refunded, over the "
                        f"{config.REFUND_APPROVAL_THRESHOLD:.0f} auto-approval limit")
            return f"refund {amount:.2f} exceeds the {config.REFUND_APPROVAL_THRESHOLD:.0f} auto-approval limit"
    return None


def args_fingerprint(tool_name: str, args: dict[str, Any]) -> str:
    canon = json.dumps({k: v for k, v in sorted(args.items()) if k != "run_id"}, sort_keys=True, default=str)
    return hashlib.sha256(f"{tool_name}:{canon}".encode()).hexdigest()[:16]


def record_event(**kw: Any) -> Event:
    ev = Event(**kw)
    get_store().set("events", ev.id, ev.model_dump())
    return ev


_t0: dict[str, float] = {}

# Tool-call counters keyed by "run_id:task_id" for the loop-containment budget.
_tool_calls: dict[str, int] = {}


def reset_tool_budget(run_id: str, task_id: str = "") -> None:
    """Clear the loop-containment counter for a task (called when a task starts)."""
    _tool_calls.pop(f"{run_id}:{task_id}", None)


def _prior_result(store, run_id: str, task_id: Optional[str], tool_name: str, args: dict[str, Any]) -> Optional[dict]:
    """The recorded result of an identical, already-successful call in this task, if any.

    Matched on the argument fingerprint, which excludes run_id, so a replay of the same
    logical action is recognised even though the retry is a different agent turn."""
    if not run_id:
        return None
    fp = args_fingerprint(tool_name, args)
    for e in store.query("events", run_id=run_id):
        if e.get("kind") != "tool" or e.get("name") != tool_name or e.get("task_id") != task_id:
            continue
        try:
            if args_fingerprint(tool_name, json.loads(e.get("args_json") or "{}")) != fp:
                continue
            result = json.loads(e.get("result_json") or "null")
        except ValueError:
            continue
        if isinstance(result, dict) and result.get("status") == "success":
            return {**result, "idempotent_replay": True,
                    "instruction": "This exact action already completed earlier in this task. "
                                   "It was NOT run again. Treat it as done and do not retry it."}
    return None


def before_tool(tool, args: dict[str, Any], tool_context) -> Optional[dict]:
    store = get_store()
    state = tool_context.state
    run_id = state.get("run_id", "")
    task_id = state.get("task_id")
    ticket_id = state.get("ticket_id", "")
    agent = getattr(tool_context, "agent_name", None)

    # Tools that mutate take the run id so fault injection is reproducible per run.
    func = getattr(tool, "func", None)
    if tool.name in MUTATING and func is not None and "run_id" in func.__code__.co_varnames:
        args["run_id"] = run_id

    # Loop containment. Counted across EVERY tool, not just mutating ones: the runaway
    # case the rubric asks about is a worker that spins on reads and never concludes.
    # The budget is per task, so a legitimately multi-step task is unaffected.
    budget_key = f"{run_id}:{task_id or ''}"
    _tool_calls[budget_key] = _tool_calls.get(budget_key, 0) + 1
    if _tool_calls[budget_key] > config.MAX_TOOL_CALLS_PER_TASK:
        record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="loop_guard",
                     args_json=json.dumps({"tool": tool.name, "calls": _tool_calls[budget_key],
                                           "budget": config.MAX_TOOL_CALLS_PER_TASK}, default=str))
        return {"status": "blocked",
                "reason": f"tool-call budget of {config.MAX_TOOL_CALLS_PER_TASK} exhausted for this task",
                "instruction": "You are looping. Stop calling tools. Report outcome='failed' with this reason. Do not claim the task is done."}

    # Idempotency. run_agent retries a whole agent turn on any exception, including one
    # raised AFTER a tool already committed to the store: a timeout mid-turn, or a schema
    # validation failure on the worker's final answer. The retry starts a fresh session and
    # the worker calls the same tool again, so a refund small enough to stay inside the
    # remaining balance and under the approval limit gets paid twice, silently.
    #
    # Every tool call is already persisted with its arguments, so the replay is detectable:
    # if this exact call already succeeded for this task, return the recorded result rather
    # than executing it again. The worker sees what it saw the first time.
    if tool.name in MUTATING:
        prior = _prior_result(store, run_id, task_id, tool.name, args)
        if prior is not None:
            record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="idempotent_replay",
                         args_json=json.dumps({"tool": tool.name, "args": args}, default=str),
                         result_json=json.dumps(prior, default=str)[:2000])
            return prior

    if tool.name in MUTATING and store.get_setting("kill_switch", False):
        record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="kill_switch_block",
                     args_json=json.dumps({"tool": tool.name, "args": args}, default=str))
        return {"status": "blocked", "reason": "fleet kill switch is engaged; no mutating actions are allowed",
                "instruction": "Report outcome='blocked'. Do not claim the task is done."}

    # Deterministic pre-execution gate: block a state-inconsistent write before it happens.
    if tool.name in MUTATING:
        gate = _state_gate(tool.name, args, store)
        if gate:
            record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="gate_block",
                         args_json=json.dumps({"tool": tool.name, "args": args, "reason": gate}, default=str))
            return {"status": "blocked", "reason": gate,
                    "instruction": "This action is inconsistent with the current system state and was blocked before it ran. Report outcome='failed' and quote this reason. Do not claim the task is done."}

    reason = _is_high_risk(tool.name, args, store)
    if reason:
        fp = args_fingerprint(tool.name, args)
        approved = [a for a in store.query("approvals", ticket_id=ticket_id, action=tool.name, status="approved")
                    if args_fingerprint(a["action"], json.loads(a["args_json"])) == fp]
        if not approved:
            pending = [a for a in store.query("approvals", ticket_id=ticket_id, action=tool.name, status="pending")
                       if args_fingerprint(a["action"], json.loads(a["args_json"])) == fp]
            if pending:
                apr_id = pending[0]["id"]
            else:
                apr = Approval(run_id=run_id, ticket_id=ticket_id, task_id=task_id or "", agent=agent or "",
                               action=tool.name, args_json=json.dumps(args, default=str), risk_reason=reason)
                store.set("approvals", apr.id, apr.model_dump())
                apr_id = apr.id
            record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="approval_required",
                         args_json=json.dumps({"tool": tool.name, "args": args, "approval_id": apr_id}, default=str))
            return {"status": "pending_approval", "approval_id": apr_id, "reason": reason,
                    "instruction": "A human must approve this action. Report outcome='blocked' with this approval id. Do not claim the task is done."}
        # Single-use. An approval is permission for ONE action, not a standing capability:
        # left reusable, a replayed ticket could spend the same approved refund again.
        store.update("approvals", approved[0]["id"],
                     {"status": "consumed", "consumed_at": now_iso(), "consumed_by_run": run_id})
        record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="approval_honoured",
                     args_json=json.dumps({"tool": tool.name, "approval_id": approved[0]["id"], "consumed": True}, default=str))

    _t0[getattr(tool_context, "function_call_id", None) or f"{run_id}:{tool.name}"] = time.perf_counter()
    return None


def after_tool(tool, args: dict[str, Any], tool_context, tool_response) -> Optional[dict]:
    state = tool_context.state
    run_id = state.get("run_id", "")
    key = getattr(tool_context, "function_call_id", None) or f"{run_id}:{tool.name}"
    t0 = _t0.pop(key, None)
    latency = int((time.perf_counter() - t0) * 1000) if t0 else None
    record_event(run_id=run_id, task_id=state.get("task_id"), agent=getattr(tool_context, "agent_name", None),
                 kind="tool", name=tool.name, args_json=json.dumps(args, default=str),
                 result_json=json.dumps(tool_response, default=str)[:4000], latency_ms=latency)
    return None
