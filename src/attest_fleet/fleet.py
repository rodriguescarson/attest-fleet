"""The orchestrator: one ticket through the full eight-step loop.

1 trigger → 2 decomposition (controller) → 3 context passing → 4 tool calls (workers)
→ 5 verification (post-conditions) → 6 evidence (Firestore) → 7 approval/rollback
(policy gate) → 8 experience capture (playbook).

Deliberately explicit Python rather than free-form agent transfer: the loop is the
auditable artefact."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional, Type, TypeVar

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel

from . import config, experience, guard, policy
from .agents import build_auditor, build_controller, build_worker
from .domain import AuditVerdict, Claim, Event, Plan, RunRecord, Task, TaskResult, Ticket, Verification, now_iso
from .store import get_store
from .verifier import verify

T = TypeVar("T", bound=BaseModel)

_agents: dict[str, Any] = {}


def _agent(name: str, model: Optional[str] = None):
    key = (name, model)
    if key not in _agents:
        if name == "fleet_controller":
            _agents[key] = build_controller(model)
        elif name == "auditor":
            _agents[key] = build_auditor(model)
        else:
            _agents[key] = build_worker(name, model)
    return _agents[key]


def _log(run_id: str, kind: str, name: str, task_id: Optional[str] = None, agent: Optional[str] = None, latency_ms: Optional[int] = None, **payload: Any) -> None:
    ev = Event(run_id=run_id, task_id=task_id, agent=agent, kind=kind, name=name, latency_ms=latency_ms,
               args_json=json.dumps(payload, default=str)[:4000])
    get_store().set("events", ev.id, ev.model_dump())


def _extract_json(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start >= 0 and end > start else text


OUTPUT_KEY = {"fleet_controller": "plan", "billing_agent": "claim", "account_agent": "claim", "auditor": "verdict"}


async def run_agent(agent_name: str, prompt: str, state: dict[str, Any], schema: Type[T], retries: int = 3) -> T:
    """Run one ADK agent for one turn and parse its structured output.

    With output_schema set, ADK writes the parsed object to session.state under the
    agent's output_key and never marks a text event 'final' — so we read state, with
    a final-text parse as a fallback."""
    chain = config.MODEL_CHAINS.get(agent_name) or [None]
    out_key = OUTPUT_KEY[agent_name]
    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        model = chain[min(attempt, len(chain) - 1)]
        agent = _agent(agent_name, model)
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, app_name=config.APP_NAME, session_service=session_service)
        session = await session_service.create_session(app_name=config.APP_NAME, user_id="fleet", state=dict(state))
        content = types.Content(role="user", parts=[types.Part(text=prompt)])
        t0 = time.perf_counter()
        final_text = ""
        try:

            async def _drain() -> str:
                seen = ""
                async for event in runner.run_async(user_id="fleet", session_id=session.id, new_message=content):
                    if event.content and event.content.parts:
                        text = "".join(p.text or "" for p in event.content.parts if getattr(p, "text", None))
                        if text:
                            seen = text
                return seen

            # Wall-clock containment: an agent that neither finishes nor raises (a tool
            # loop, a stalled stream) is cut off here rather than running until the Cloud
            # Run request timeout. The timeout is an exception like any other, so the
            # retry-and-model-fallback path below handles it.
            final_text = await asyncio.wait_for(_drain(), timeout=config.AGENT_TURN_TIMEOUT_S)
            latency = int((time.perf_counter() - t0) * 1000)
            done = await session_service.get_session(app_name=config.APP_NAME, user_id="fleet", session_id=session.id)
            payload = done.state.get(out_key) if done else None
            _log(state.get("run_id", ""), "model", "final_response", task_id=state.get("task_id"), agent=agent_name, latency_ms=latency, model=model, via=("state" if payload else "text"))
            if payload:
                return schema.model_validate(payload)
            if final_text.strip():
                return schema.model_validate_json(_extract_json(final_text))
            raise ValueError(f"{agent_name} produced no structured output")
        except Exception as e:  # noqa: BLE001 — retry on rate limits and transient model errors
            last_err = e
            _log(state.get("run_id", ""), "model", "error", task_id=state.get("task_id"), agent=agent_name, attempt=attempt, model=model, error=str(e)[:400])
            if attempt < retries:
                switching = chain[min(attempt + 1, len(chain) - 1)] != model
                await asyncio.sleep(0.4 if switching else 1.5 * (attempt + 1))
    raise RuntimeError(f"{agent_name} failed after {retries + 1} attempts: {last_err}")


def _ticket_prompt(ticket: Ticket, vision: Optional[str] = None) -> str:
    base = (f"TICKET {ticket.id} (source: {ticket.source})\n"
            f"Customer reference: {ticket.customer_ref}\n"
            f"Subject: {ticket.subject}\n\n{ticket.body}")
    if vision:
        base += f"\n\nATTACHED IMAGE (read by the vision model): {vision}"
    return base


def _task_prompt(task: Task) -> str:
    return "TASK\n" + json.dumps(task.model_dump(exclude={"rationale"}), indent=2)


async def _audit(run_id: str, task: Task, claim: Optional[Claim]) -> Verification:
    store = get_store()
    events = [e for e in store.query("events", run_id=run_id) if e.get("task_id") == task.id and e.get("kind") == "tool"]
    prompt = "TASK\n" + task.model_dump_json(indent=2) + "\n\nCLAIM\n" + (claim.model_dump_json(indent=2) if claim else "none") + \
             "\n\nTOOL EVENTS\n" + json.dumps([{"tool": e["name"], "args": e["args_json"], "result": e["result_json"]} for e in events], indent=2)
    try:
        verdict = await run_agent("auditor", prompt, {"run_id": run_id, "task_id": task.id}, AuditVerdict)
    except Exception as e:  # noqa: BLE001
        return Verification(task_id=task.id, verified=None, method="auditor", detail=f"auditor failed: {e}")
    claimed_done = bool(claim and claim.outcome == "done")
    return Verification(task_id=task.id, verified=verdict.verified, method="auditor",
                        silent_failure=claimed_done and not verdict.verified,
                        false_alarm=(claim is not None and not claimed_done and verdict.verified), detail=verdict.reasoning)


async def run_ticket(ticket: Ticket) -> RunRecord:
    store = get_store()
    run = RunRecord(ticket=ticket)
    store.set("runs", run.id, run.model_dump())
    _log(run.id, "run", "started", ticket_id=ticket.id)
    agent_ticket = ticket.model_copy(update={"expected": None})  # ground truth never reaches an agent
    try:
        # Model Armor screens the untrusted ticket text before any agent sees it.
        verdict = guard.screen(f"{ticket.subject}\n\n{ticket.body}")
        _log(run.id, "policy", "model_armor", ticket_id=ticket.id, **verdict)
        if verdict["blocked"]:
            run.status = "failed"
            run.error = f"blocked by Model Armor: {verdict['reason']} ({verdict.get('confidence')})"
            store.set("runs", run.id, run.model_dump())
            return run
        if store.get_setting("kill_switch", False):
            run.status = "killed"
            run.error = "kill switch engaged"
            return run
        vision_desc: Optional[str] = None
        if ticket.image_url:
            from .vision import read_attachment
            t0 = time.perf_counter()
            try:
                vision_desc = await read_attachment(ticket.image_url)
                run.vision = vision_desc
                _log(run.id, "model", "vision_read", agent="vision_reader", latency_ms=int((time.perf_counter() - t0) * 1000), saw=vision_desc)
                store.set("runs", run.id, run.model_dump())
            except Exception as e:  # noqa: BLE001 — a bad attachment must not sink the ticket
                _log(run.id, "model", "vision_error", agent="vision_reader", error=str(e)[:300])
        plan = await run_agent("fleet_controller", _ticket_prompt(agent_ticket, vision_desc), {"run_id": run.id, "ticket_id": ticket.id}, Plan)
        run.plan = plan
        store.set("runs", run.id, run.model_dump())
        pairs: list[tuple[Claim, Verification]] = []
        for task in plan.tasks:
            state = {"run_id": run.id, "ticket_id": ticket.id, "task_id": task.id}
            policy.reset_tool_budget(run.id, task.id)  # fresh loop-containment budget per task
            claim: Optional[Claim] = None
            try:
                claim = await run_agent(task.worker, _task_prompt(task), state, Claim)
                claim.task_id = task.id
            except Exception as e:  # noqa: BLE001
                _log(run.id, "run", "worker_error", task_id=task.id, agent=task.worker, error=str(e)[:500])
            verification = verify(store, task, claim)
            if verification.method == "none":
                verification = await _audit(run.id, task, claim)
            _log(run.id, "verify", "verified", task_id=task.id, verified=verification.verified, silent=verification.silent_failure, detail=verification.detail[:500])
            sig = experience.capture(store, run.id, task, claim, verification)
            run.results.append(TaskResult(task=task, claim=claim, verification=verification))
            if claim:
                pairs.append((claim, verification))
            store.set("runs", run.id, run.model_dump())
        if any(r.verification and r.verification.silent_failure for r in run.results):
            run.status = "silent_failure"
        elif any(r.claim and r.claim.outcome == "blocked" for r in run.results):
            run.status = "pending_approval"
        elif all(r.verification and r.verification.verified for r in run.results) and run.results:
            run.status = "verified"
        else:
            run.status = "failed"
        if ticket.expected is not None:
            run.ground_truth = _ground_truth(ticket)
    except Exception as e:  # noqa: BLE001
        run.status = "failed"
        run.error = str(e)[:1000]
        _log(run.id, "run", "error", error=run.error)
    finally:
        run.finished_at = now_iso()
        store.set("runs", run.id, run.model_dump())
        _log(run.id, "run", "finished", status=run.status)
    return run


def _ground_truth(ticket: Ticket) -> Optional[bool]:
    """Eval harness only: did the world end up as the ticket author intended?"""
    store = get_store()
    exp = ticket.expected
    if exp is None:
        return None
    if exp.should_block:
        # The correct end state is: nothing mutated, an approval pending.
        pend = store.query("approvals", ticket_id=ticket.id, status="pending")
        untouched = True
        if exp.order_id:
            o = store.get("orders", exp.order_id)
            untouched = untouched and (o is not None and float(o.get("refunded", 0)) == 0.0)
        return bool(pend) and untouched
    fake = Task(id="gt", type=exp.task_type, worker="account_agent", customer_id=exp.customer_id, order_id=exp.order_id,
                subscription_id=exp.subscription_id, amount=exp.amount, new_address=exp.new_address, instruction="", rationale="")
    v = verify(store, fake, None)
    if v.verified is None:
        return None
    # Also make sure the *other* Priya was not touched.
    if exp.customer_id:
        for c in store.query("customers", name=(store.get("customers", exp.customer_id) or {}).get("name")):
            if c["id"] != exp.customer_id and (c.get("address_draft") or c.get("deleted")):
                return False
    return v.verified
