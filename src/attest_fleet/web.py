"""HTTP surface: the trigger endpoint, the evidence API and a small operator dashboard.

Runs on Cloud Run. POST /tickets is the enterprise trigger (webhook or Pub/Sub push);
everything else is what a fleet operator needs: runs, evidence, approvals, kill switch,
metrics, and the agent identity list."""

from __future__ import annotations

import asyncio
import base64
import html
import json
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from . import config, metrics
from .agents import AGENT_IDENTITIES
from .domain import Claim, RunRecord, Ticket, Verification, now_iso
from .fleet import run_ticket
from .store import get_store, seed


@asynccontextmanager
async def lifespan(app: FastAPI):
    seed(get_store())
    yield


app = FastAPI(title="Attest Fleet", version="0.1.0", lifespan=lifespan)

if config.ADK_UI:
    try:  # the ADK developer UI, mounted for local demos
        import os

        from google.adk.cli.fast_api import get_fast_api_app

        adk_app = get_fast_api_app(agents_dir=os.path.join(os.path.dirname(__file__), "..", "..", "agents"), web=True, url_prefix="/adk")
        app.mount("/adk", adk_app)
    except Exception as e:  # noqa: BLE001
        print("ADK UI not mounted:", e)


# ------------------------------------------------------------------ triggers


@app.post("/tickets", status_code=202)
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


# ------------------------------------------------------------------ evidence API


def _pairs(runs: list[dict]) -> list[tuple[Claim, Verification]]:
    out = []
    for r in runs:
        for tr in r.get("results", []):
            if tr.get("claim") and tr.get("verification"):
                out.append((Claim.model_validate(tr["claim"]), Verification.model_validate(tr["verification"])))
    return out


@app.get("/runs")
def list_runs(limit: int = 50) -> list[dict]:
    runs = get_store().list("runs", limit=limit)
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs


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


@app.post("/approvals/{approval_id}/{decision}")
def decide_approval(approval_id: str, decision: str, decided_by: str = "operator") -> dict:
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision must be approve|reject")
    store = get_store()
    a = store.get("approvals", approval_id)
    if not a:
        raise HTTPException(404)
    store.update("approvals", approval_id, {"status": "approved" if decision == "approve" else "rejected", "decided_at": now_iso(), "decided_by": decided_by})
    return store.get("approvals", approval_id)


@app.post("/approvals/{approval_id}/{decision}/rerun", status_code=202)
async def decide_and_rerun(approval_id: str, decision: str, background: BackgroundTasks) -> dict:
    """Approve (or reject) and re-run the originating ticket so the fleet finishes the job."""
    a = decide_approval(approval_id, decision)
    run = get_store().get("runs", a["run_id"])
    if not run:
        raise HTTPException(404, "originating run missing")
    ticket = Ticket.model_validate(run["ticket"])
    background.add_task(run_ticket, ticket)
    return {"approval": a, "rerun_ticket_id": ticket.id}


@app.post("/fleet/kill")
def kill() -> dict:
    get_store().set_setting("kill_switch", True)
    return {"kill_switch": True}


@app.post("/fleet/resume")
def resume() -> dict:
    get_store().set_setting("kill_switch", False)
    return {"kill_switch": False}


@app.get("/fleet/identities")
def identities() -> list[dict]:
    return AGENT_IDENTITIES


@app.get("/fleet/playbook")
def playbook() -> list[dict]:
    return get_store().list("playbook", limit=100)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "store": get_store().backend, "models": {"controller": config.CONTROLLER_MODEL, "worker": config.WORKER_MODEL}}


# ------------------------------------------------------------------ dashboard


def _fmt(v: Any) -> str:
    return "—" if v is None else (f"{v:.1%}" if isinstance(v, float) and v <= 1 else str(v))


@app.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    store = get_store()
    m = get_metrics()
    runs = list_runs(limit=25)
    pending = list_approvals("pending")
    kill_on = bool(store.get_setting("kill_switch", False))
    e = html.escape
    rows = "".join(
        f"<tr><td><a href='/runs/{e(r['id'])}'>{e(r['id'])}</a></td><td>{e(r['ticket']['subject'])}</td>"
        f"<td class='s-{e(r['status'])}'>{e(r['status'])}</td><td>{len(r.get('results', []))}</td>"
        f"<td>{'—' if r.get('ground_truth') is None else ('pass' if r['ground_truth'] else 'FAIL')}</td><td>{e(r.get('started_at', '')[:19])}</td></tr>"
        for r in runs)
    aprs = "".join(
        f"<tr><td>{e(a['id'])}</td><td>{e(a['agent'])}</td><td>{e(a['action'])}</td><td><code>{e(a['args_json'])}</code></td><td>{e(a['risk_reason'])}</td>"
        f"<td><form method='post' action='/approvals/{e(a['id'])}/approve/rerun'><button>Approve + rerun</button></form>"
        f"<form method='post' action='/approvals/{e(a['id'])}/reject'><button>Reject</button></form></td></tr>" for a in pending)
    esc = m.get("escalation")
    esc_txt = f"escalate below confidence {esc['threshold']:.2f} → auto-accept {esc['coverage']:.0%} of claims at {esc['risk']:.1%} residual risk" if esc else "not enough verified claims yet"
    return f"""<!doctype html><meta charset=utf-8><title>Attest Fleet</title>
<style>body{{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#16211d;background:#f4f5f2}}
h1{{margin:0}} .sub{{color:#556}} .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.5rem;margin:1rem 0}}
.stat{{background:#fff;border:1px solid #d9ddd8;padding:.6rem .8rem;border-radius:4px}} .stat b{{display:block;font-size:1.4rem}} .stat span{{font-size:.75rem;text-transform:uppercase;letter-spacing:.08em;color:#667}}
table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px}} th,td{{text-align:left;padding:.45rem .6rem;border-bottom:1px solid #e3e6e2;vertical-align:top}} th{{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:#667}}
.s-verified{{color:#1f5c42;font-weight:600}} .s-silent_failure{{color:#a32014;font-weight:600}} .s-pending_approval{{color:#8a5000;font-weight:600}} .s-failed,.s-killed{{color:#667}}
button{{font:inherit;padding:.25rem .6rem;margin:.1rem}} form{{display:inline}} .kill{{background:#a32014;color:#fff;border:0}} .ok{{background:#1f5c42;color:#fff;border:0}} code{{font-size:12px}}
h2{{margin-top:2rem;font-size:1.1rem}}</style>
<h1>Attest Fleet</h1><p class=sub>Governed agent fleet on Gemini 3.5 + ADK + Cloud Run + Firestore. Store: <b>{e(store.backend)}</b>.
Kill switch: <b>{'ENGAGED' if kill_on else 'off'}</b>
<form method=post action='/fleet/{'resume' if kill_on else 'kill'}'><button class='{'ok' if kill_on else 'kill'}'>{'Resume fleet' if kill_on else 'Kill switch'}</button></form></p>
<div class=grid>
<div class=stat><span>Runs</span><b>{m['runs']}</b></div>
<div class=stat><span>Reported success</span><b>{_fmt(m['reported_success_rate'])}</b></div>
<div class=stat><span>Verified success</span><b>{_fmt(m['verified_success_rate'])}</b></div>
<div class=stat><span>Silent failure rate</span><b style='color:#a32014'>{_fmt(m['silent_failure_rate'])}</b></div>
<div class=stat><span>False alarms</span><b>{_fmt(m['false_alarm_rate'])}</b></div>
<div class=stat><span>Brier / ECE</span><b>{_fmt(m['brier'])} / {_fmt(m['ece'])}</b></div>
<div class=stat><span>Eval ground truth</span><b>{_fmt(m['eval_ground_truth_pass_rate'])}</b></div>
</div>
<p><b>Escalation policy:</b> {e(esc_txt)}.</p>
<h2>Pending approvals ({len(pending)})</h2>
<table><tr><th>id</th><th>agent</th><th>action</th><th>args</th><th>why</th><th></th></tr>{aprs or '<tr><td colspan=6>none</td></tr>'}</table>
<h2>Recent runs</h2>
<table><tr><th>run</th><th>ticket</th><th>status</th><th>tasks</th><th>eval</th><th>started</th></tr>{rows or '<tr><td colspan=6>none yet — POST /tickets</td></tr>'}</table>
<p class=sub>API: <a href='/metrics'>/metrics</a> · <a href='/runs'>/runs</a> · <a href='/approvals'>/approvals</a> · <a href='/fleet/identities'>/fleet/identities</a> · <a href='/fleet/playbook'>/fleet/playbook</a> · <a href='/docs'>/docs</a></p>"""


@app.post("/fleet/kill", include_in_schema=False)
def kill_form():
    kill()
    return RedirectResponse("/", status_code=303)


@app.post("/fleet/resume", include_in_schema=False)
def resume_form():
    resume()
    return RedirectResponse("/", status_code=303)
