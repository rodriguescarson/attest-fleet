"""The governance layer: every tool call passes through here.

before_tool: kill switch, high-risk approval gate, run-id injection.
after_tool:  execution-evidence capture (args, result, latency) to the store.

Both are ADK callbacks attached to every worker agent."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from . import config
from .domain import Approval, Event
from .store import get_store
from .tools import MUTATING


def _is_high_risk(tool_name: str, args: dict[str, Any]) -> Optional[str]:
    if tool_name == "delete_account":
        return "account deletion is irreversible"
    if tool_name == "issue_refund":
        try:
            amount = float(args.get("amount", 0))
        except (TypeError, ValueError):
            amount = 0.0
        if amount > config.REFUND_APPROVAL_THRESHOLD:
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


def before_tool(tool, args: dict[str, Any], tool_context) -> Optional[dict]:
    store = get_store()
    state = tool_context.state
    run_id = state.get("run_id", "")
    task_id = state.get("task_id")
    ticket_id = state.get("ticket_id", "")
    agent = getattr(tool_context, "agent_name", None)

    # Tools that mutate take the run id so fault injection is reproducible per run.
    if tool.name in MUTATING and "run_id" in getattr(tool, "func", lambda: None).__code__.co_varnames if hasattr(tool, "func") else False:
        args["run_id"] = run_id

    if tool.name in MUTATING and store.get_setting("kill_switch", False):
        record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="kill_switch_block",
                     args_json=json.dumps({"tool": tool.name, "args": args}, default=str))
        return {"status": "blocked", "reason": "fleet kill switch is engaged; no mutating actions are allowed",
                "instruction": "Report outcome='blocked'. Do not claim the task is done."}

    reason = _is_high_risk(tool.name, args)
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
        record_event(run_id=run_id, task_id=task_id, agent=agent, kind="policy", name="approval_honoured",
                     args_json=json.dumps({"tool": tool.name, "approval_id": approved[0]["id"]}, default=str))

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
