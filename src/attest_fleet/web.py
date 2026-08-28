"""HTTP surface: the trigger endpoint, the evidence API and a small operator dashboard.

Runs on Cloud Run. POST /tickets is the enterprise trigger (webhook or Pub/Sub push);
everything else is what a fleet operator needs: runs, evidence, approvals, kill switch,
metrics, and the agent identity list."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import secrets
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, metrics
from .agents import AGENT_IDENTITIES
from .domain import Claim, RunRecord, Ticket, Verification, now_iso
from .fleet import run_ticket
from .store import get_store, seed


def _setup_tracing() -> None:
    """Export ADK's OpenTelemetry GenAI spans to Cloud Trace.

    ADK already emits reasoning-chain spans under the OTel GenAI semantic conventions;
    this just points them at the project so the agent trail is inspectable in Cloud Trace
    alongside the evidence store. Best-effort: tracing must never keep the fleet down."""
    if os.getenv("ATTEST_TRACING", "1") != "1" or not config.VERTEX_PROJECT:
        return
    try:
        from google.adk.telemetry.google_cloud import get_gcp_exporters
        from google.adk.telemetry.setup import maybe_set_otel_providers

        os.environ.setdefault("OTEL_SERVICE_NAME", "attest-fleet")
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", config.VERTEX_PROJECT)
        maybe_set_otel_providers([get_gcp_exporters(enable_cloud_tracing=True)])
        print("tracing: exporting OTel GenAI spans to Cloud Trace")
    except Exception as e:  # noqa: BLE001
        print("tracing not enabled:", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    _setup_tracing()
    seed(get_store())
    if os.getenv("ATTEST_DEMO") == "1":
        from .demo import seed_demo
        seed_demo(get_store())
    yield


app = FastAPI(title="Attest Fleet", version="0.1.0", lifespan=lifespan)


# ------------------------------------------------------------------ operator boundary

OPERATOR_TOKEN = os.getenv("ATTEST_OPERATOR_TOKEN", "")


def require_operator(request: Request) -> None:
    """Guard every state-changing endpoint.

    A fleet whose kill switch and approval gate are world-writable has no governance at
    all, so mutation is separated from inspection: reads (runs, metrics, evidence,
    identities) stay open for auditability, writes require the operator token.

    Unset token = local development, everything allowed. In production the token is set
    on Cloud Run. A rejected call is written to the evidence trail like any other
    policy decision, so an attempted unauthorised mutation is visible to the operator.
    """
    if not OPERATOR_TOKEN:
        return
    supplied = request.headers.get("x-attest-token") or request.query_params.get("token") or ""
    if not secrets.compare_digest(supplied, OPERATOR_TOKEN):
        try:
            from .policy import record_event

            record_event(
                run_id="fleet",
                kind="policy",
                name="auth_denied",
                args_json=json.dumps({"path": request.url.path, "method": request.method}),
                result_json=json.dumps({"reason": "missing or invalid operator token"}),
            )
        except Exception:  # noqa: BLE001 - never let audit logging block the 403
            pass
        raise HTTPException(403, "operator token required for state-changing calls")

if config.ADK_UI:
    try:  # the ADK developer UI, mounted for local demos
        import os

        from google.adk.cli.fast_api import get_fast_api_app

        adk_app = get_fast_api_app(agents_dir=os.path.join(os.path.dirname(__file__), "..", "..", "agents"), web=True, url_prefix="/adk")
        app.mount("/adk", adk_app)
    except Exception as e:  # noqa: BLE001
        print("ADK UI not mounted:", e)


# ------------------------------------------------------------------ triggers


@app.post("/tickets", status_code=202, dependencies=[Depends(require_operator)])
async def create_ticket(request: Request, background: BackgroundTasks) -> dict:
    """Enterprise trigger. Accepts a Ticket JSON body, or a Pub/Sub push envelope
    whose message.data is a base64 Ticket JSON."""
    payload = await request.json()
    if "message" in payload and "data" in payload["message"]:
        payload = json.loads(base64.b64decode(payload["message"]["data"]).decode())
    ticket = Ticket.model_validate(payload)
    if request.query_params.get("wait") == "1":
        run = await run_ticket(ticket)
        return run.model_dump()
    background.add_task(run_ticket, ticket)
    return {"accepted": True, "ticket_id": ticket.id}


@app.post("/tickets/batch", status_code=202, dependencies=[Depends(require_operator)])
async def create_tickets_batch(request: Request, background: BackgroundTasks) -> dict:
    """Async batch trigger: enqueue many tickets at once; they process in the
    background and stream onto the dashboard. Accepts {"tickets": [...]} or a bare list."""
    payload = await request.json()
    items = payload.get("tickets", payload) if isinstance(payload, dict) else payload
    ids = []
    for raw in (items or [])[:200]:
        tk = Ticket.model_validate(raw)
        background.add_task(run_ticket, tk)
        ids.append(tk.id)
    return {"accepted": len(ids), "ticket_ids": ids}


@app.post("/audit")
async def audit(request: Request) -> dict:
    """Framework-agnostic batch audit. POST a list of agent-run logs — each
    {claimed_done|outcome, confidence, verified} — and get the Attest report
    (silent-failure rate, calibration, escalation) over the whole batch. No agents
    are re-run: this is pure measurement, so it scales to large volumes of logs from
    any agent stack (point OpenTelemetry GenAI spans or your own logs at it)."""
    payload = await request.json()
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    return metrics.compute_records(records or [], config.TARGET_RESIDUAL_RISK)


# ------------------------------------------------------------------ evidence API


def _pairs(runs: list[dict]) -> list[tuple[Claim, Verification]]:
    out = []
    for r in runs:
        for tr in r.get("results", []):
            if tr.get("claim") and tr.get("verification"):
                out.append((Claim.model_validate(tr["claim"]), Verification.model_validate(tr["verification"])))
    return out


@app.get("/runs")
def list_runs(q: str = "", status: str = "", sort: str = "started", order: str = "desc", limit: int = 20, offset: int = 0) -> dict:
    """Server-side search, filter, sort and pagination over runs."""
    runs = get_store().list("runs", limit=10_000)
    if status:
        runs = [r for r in runs if r.get("status") == status]
    if q:
        ql = q.lower()
        runs = [r for r in runs if ql in r.get("id", "").lower() or ql in r.get("ticket", {}).get("subject", "").lower() or ql in r.get("ticket", {}).get("customer_ref", "").lower()]
    keyers = {
        "started": lambda r: r.get("started_at", ""),
        "subject": lambda r: r.get("ticket", {}).get("subject", "").lower(),
        "status": lambda r: r.get("status", ""),
        "tasks": lambda r: len(r.get("results", [])),
    }
    runs.sort(key=keyers.get(sort, keyers["started"]), reverse=(order != "asc"))
    total = len(runs)
    limit = max(1, min(limit, 100))
    return {"items": runs[offset:offset + limit], "total": total, "limit": limit, "offset": offset,
            "counts": {s: sum(1 for r in get_store().list("runs", limit=10_000) if r.get("status") == s) for s in ("verified", "silent_failure", "pending_approval", "failed", "killed", "running")}}


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    r = get_store().get("runs", run_id)
    if not r:
        raise HTTPException(404)
    events = sorted(get_store().query("events", run_id=run_id), key=lambda e: e.get("ts", ""))
    return {**r, "events": events}


@app.get("/metrics")
def get_metrics() -> dict:
    runs = get_store().list("runs", limit=5000)
    m = metrics.compute(_pairs(runs), config.TARGET_RESIDUAL_RISK)
    gt = [r["ground_truth"] for r in runs if r.get("ground_truth") is not None]
    m["eval_ground_truth_pass_rate"] = round(sum(1 for g in gt if g) / len(gt), 4) if gt else None
    m["runs"] = len(runs)
    m["by_status"] = {s: sum(1 for r in runs if r.get("status") == s) for s in ("verified", "silent_failure", "pending_approval", "failed", "killed", "running")}
    return m


@app.get("/approvals")
def list_approvals(status: Optional[str] = None) -> list[dict]:
    store = get_store()
    rows = store.query("approvals", status=status) if status else store.list("approvals", limit=500)
    rows.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return rows


@app.post("/approvals/{approval_id}/{decision}", dependencies=[Depends(require_operator)])
def decide_approval(approval_id: str, decision: str, decided_by: str = "operator") -> dict:
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    store = get_store()
    a = store.get("approvals", approval_id)
    if not a:
        raise HTTPException(404)
    store.update("approvals", approval_id, {"status": "approved" if decision == "approve" else "rejected", "decided_at": now_iso(), "decided_by": decided_by})
    return store.get("approvals", approval_id)


@app.post("/approvals/{approval_id}/{decision}/rerun", status_code=202, dependencies=[Depends(require_operator)])
async def decide_and_rerun(approval_id: str, decision: str, background: BackgroundTasks) -> dict:
    """Approve (or reject) and re-run the originating ticket so the fleet finishes the job."""
    a = decide_approval(approval_id, decision)
    run = get_store().get("runs", a["run_id"])
    if not run:
        raise HTTPException(404, "originating run missing")
    ticket = Ticket.model_validate(run["ticket"])
    background.add_task(run_ticket, ticket)
    return {"approval": a, "rerun_ticket_id": ticket.id}


@app.post("/fleet/kill", dependencies=[Depends(require_operator)])
def kill() -> dict:
    get_store().set_setting("kill_switch", True)
    return {"kill_switch": True}


@app.post("/fleet/resume", dependencies=[Depends(require_operator)])
def resume() -> dict:
    get_store().set_setting("kill_switch", False)
    return {"kill_switch": False}


@app.post("/admin/seed", include_in_schema=False)
def admin_seed(token: str = "") -> dict:
    """Reset the board to the curated demo runs. Token-guarded; demo project only."""
    expected = os.getenv("ATTEST_ADMIN_TOKEN")
    if not expected or token != expected:
        raise HTTPException(403, "forbidden")
    from .store import reset_evidence
    from .demo import seed_demo
    store = get_store()
    reset_evidence(store)
    seed(store, force=True)
    seed_demo(store, force=True)
    return {"ok": True, "runs": store.count("runs"), "approvals": store.count("approvals"), "playbook": store.count("playbook")}


@app.get("/fleet/identities")
def fleet_identities() -> dict:
    """The agent registry view. Read live from Google's Agent Registry when it answers,
    with the in-code identity list as the fallback and as the source of deployment facts
    (model, whether the agent mutates) that the registry does not carry."""
    from .registry import registered_agents

    agents, source = registered_agents()
    return {"source": source, "count": len(agents), "agents": agents}


@app.get("/fleet/playbook")
def playbook() -> list[dict]:
    return get_store().list("playbook", limit=100)


@app.get("/health")
def healthz() -> dict:
    store = get_store()
    return {"ok": True, "store": store.backend, "kill_switch": bool(store.get_setting("kill_switch", False)),
            "models": {"controller": config.CONTROLLER_MODEL, "worker": config.WORKER_MODEL}}


# ------------------------------------------------------------------ dashboard


def _fmt(v: Any) -> str:
    return "—" if v is None else (f"{v:.1%}" if isinstance(v, float) and v <= 1 else str(v))


_STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard() -> HTMLResponse:
    page = Path(os.path.join(_STATIC, "index.html")).read_text(encoding="utf-8")
    # The console is the operator's own client, so it is handed the token. This is a
    # published demo credential (see README): the point it demonstrates is that the
    # boundary is enforced server-side, not that this particular token is a secret.
    if OPERATOR_TOKEN:
        page = page.replace(
            "</head>",
            f"<script>window.__ATTEST_TOKEN__={json.dumps(OPERATOR_TOKEN)};</script></head>",
            1,
        )
    return HTMLResponse(page)
