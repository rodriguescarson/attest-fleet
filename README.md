# Attest Fleet

[![CI](https://github.com/rodriguescarson/attest-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/rodriguescarson/attest-fleet/actions/workflows/ci.yml)

**A governed agent fleet whose every claim is independently verified.**
Built for the All Things Agentic Hackathon (Google) — Fortified Enterprise Fleet.

## Live on Google Cloud Run

### https://attest-fleet-434066362046.asia-south1.run.app

Deployed from this repo with `deploy.sh` (Cloud Run, `asia-south1`, Firestore database
`attest-fleet`, Gemini key in Secret Manager). Nothing to install to look at it:

| Surface | URL |
|---|---|
| Operator dashboard | [`/`](https://attest-fleet-434066362046.asia-south1.run.app/) |
| Metrics (reported vs verified, Brier/ECE, escalation) | [`/metrics`](https://attest-fleet-434066362046.asia-south1.run.app/metrics) |
| Run evidence | [`/runs`](https://attest-fleet-434066362046.asia-south1.run.app/runs) |
| Agent identity list | [`/fleet/identities`](https://attest-fleet-434066362046.asia-south1.run.app/fleet/identities) |
| Health + live model config | [`/health`](https://attest-fleet-434066362046.asia-south1.run.app/health) |
| OpenAPI | [`/docs`](https://attest-fleet-434066362046.asia-south1.run.app/docs) |

The dashboard's **Example data** toggle (top right) fills the board with sample runs, so the
interface is explorable even when the live service has processed nothing recently.

Every read above is open, so the whole evidence trail is auditable without a credential.
State-changing calls on the hosted service (`POST /tickets`, the kill switch, approvals) are
behind an operator token, sent as an `x-attest-token` header or a `?token=` parameter; the
hosted dashboard supplies it for you. Locally the token is unset and nothing is gated.

**Testing credential for judges.** The demo token is published on purpose, so the project is
testable without restriction:

```
attest-operator-cc3545eb9ca5
```

```bash
URL=https://attest-fleet-434066362046.asia-south1.run.app
curl -X POST "$URL/fleet/kill"                                    # 403, no token
curl -X POST "$URL/fleet/kill?token=attest-operator-cc3545eb9ca5" # 200, kill switch engaged
curl -X POST "$URL/fleet/resume?token=attest-operator-cc3545eb9ca5"
```

A rejected call is written to the evidence trail as an `auth_denied` event, so an attempted
unauthorised mutation is visible to the operator alongside every other policy decision. The
point this demonstrates is that the boundary is enforced server-side — a fleet whose kill
switch and approval gate are world-writable has no governance at all. In a real deployment
this is Cloud Run IAM and a per-operator identity, not a shared string.

![Attest Fleet architecture: trigger, fleet controller, workers, policy gate, verifier, evidence, metrics and experience capture](docs/img/architecture.jpg)

Every agent framework reports success from the agent's own self-report. When a worker
says "refund issued" and the gateway left it `pending_gateway`, the dashboard stays green
and the customer never gets their money. Attest Fleet runs a customer-operations fleet on
Gemini 3.7 + ADK and treats every worker's "done" as a **claim** to be checked against the
system of record — then measures the gap.

```
trigger ──► fleet_controller ──► billing_agent / account_agent ──► tools (Firestore)
 (webhook,     decomposes,          execute ONE task each,           │
  Pub/Sub)     resolves customer    report a Claim                   ▼
                                                             policy gate: kill switch,
                                                             approval for high-risk actions
                                          ▼
                                     verifier: reads the system of record, checks
                                     post-conditions → silent-failure / false-alarm
                                          ▼
                     evidence (runs, events, approvals) ──► metrics: reported vs verified,
                                          ▼                   Brier / ECE, risk-coverage,
                     experience: failure → playbook lesson    escalation threshold
                                 → injected into the worker's next instruction
```

## What makes it different

Every agent-observability tool grades the agent on what it **said** (traces, an LLM-judge).
LLM-judge evaluation catches false success at **AUROC ≤ 0.65** — barely above chance (Advani,
2606.09863). Attest Fleet grades the agent on what actually **changed in the system of record**,
and turns the gap into a measured metric. It **composes with the Google stack** rather than
replacing it: **Model Armor** guards the input, ADK agents act on **Cloud Run**, **Attest
verifies against Firestore**, and **Vertex Gen AI Eval / OpenTelemetry** observe — none of which
verify, at runtime, that a claimed outcome is real.

For the Fortified Enterprise Fleet track, the pieces map to the platform vocabulary: the agent
**identity list** (`/fleet/identities`) is the agent **registry + identity**; **Firestore** is the
**memory bank** (durable cross-session context and evidence); the **policy gate** (kill switch +
pre-execution state gate + human approval) is the **agent gateway / guardrails**; and the
operator **dashboard** is **agent observability** — now measuring correctness, not just traffic.

## Stack (hackathon gate)

| Requirement | Used |
|---|---|
| Gemini 3.5+ | `gemini-3.7-flash` for the controller, both workers and vision intake |
| Google agent framework | Agent Development Kit (`google-adk` 2.7) — `LlmAgent`, tools, `output_schema`, tool callbacks |
| Google Cloud infra | Cloud Run (service), Firestore (system of record + evidence store) |
| Extra Google models (bonus) | **Gemma** (`gemma-4-31b-it`) as the independent auditor; **Gemini vision** for image intake — both motivated, not bolted on |

A brand-new Flash tier gets demand-shed (HTTP 503) in the weeks after launch, so every
Gemini role runs a fallback cascade rather than failing a ticket:
**`gemini-3.7-flash` → `gemini-3.6-flash` → `gemini-3.5-flash-lite`**. The auditor falls
back inside the Gemma family (`gemma-4-31b-it` → `gemma-4-26b-a4b-it`) so it stays a
different model family from the workers and keeps its independence. Every model id is
env-overridable (`ATTEST_CONTROLLER_MODEL`, `ATTEST_WORKER_MODEL`, `ATTEST_AUDITOR_MODEL`,
`ATTEST_VISION_MODEL`); the chains live in `src/attest_fleet/config.py` and the live
selection is served at `/health`.

## Run locally

### Path A: zero credentials, no Google account

`ATTEST_DEMO=1` seeds the in-memory store with sample runs at boot (a verified refund, a
silent failure, a pending approval, a tool error, a vision-read ticket) so the dashboard
comes up populated instead of empty. No API key, no cloud, no LLM calls.

```bash
uv sync
uv run pytest -q                                       # full suite, no credentials, no network
ATTEST_DEMO=1 ATTEST_STORE=memory \
  uv run uvicorn attest_fleet.web:app --port 8080
```

Open http://localhost:8080. Metrics, run evidence, the pending-approval queue, the kill
switch and the playbook are all filled in and clickable. The sample data is fabricated and
lives only in memory (`src/attest_fleet/demo.py`, guarded so it can never write to a real
Firestore); the same runs sit behind the **Example data** toggle on the hosted dashboard.

`uv run pytest -q` is the credential-free correctness check: metrics arithmetic, verifier
post-conditions and the policy gate, with no model in the loop.

### Path B: drive the real fleet (needs a Gemini key)

```bash
cp .env.example .env            # add GOOGLE_API_KEY (AI Studio) — ATTEST_STORE=memory
uv run uvicorn attest_fleet.web:app --reload --port 8080
```

Send a trigger (this one calls Gemini, so it needs the key from `.env`):

```bash
curl -s localhost:8080/tickets?wait=1 -H 'content-type: application/json' -d '{
  "customer_ref": "Priya Sharma",
  "subject": "Refund last month",
  "body": "Please refund my Pro monthly charge of 49. My email is priya.sharma@example.com."
}' | jq .status
```

Then open http://localhost:8080 for the operator dashboard (metrics, pending approvals,
kill switch, run evidence).

## Where things live

```
src/attest_fleet/
  web.py         HTTP surface: POST /tickets trigger, evidence API, metrics, dashboard
  fleet.py       the orchestrator: one ticket through the whole eight-step loop
  agents.py      the ADK agent definitions and the identity list (/fleet/identities)
  tools.py       FunctionTools over the system of record, plus the fault injector
  policy.py      the governance layer: kill switch, pre-execution state gate, approvals
  verifier.py    step 5: post-conditions read from the store, never the self-report
  metrics.py     reported vs verified, silent-failure rate, Brier/ECE, risk-coverage
  experience.py  step 8: a verified failure becomes a playbook lesson for the next run
  store.py       MemoryStore and FirestoreStore behind one interface
  domain.py      the typed contracts (Ticket, Plan, Task, Claim, Verification, Approval)
  config.py      env-driven runtime config: model chains, thresholds, store backend
  vision.py      multimodal intake: reads an image attached to a ticket into text
  demo.py        fabricated sample runs for ATTEST_DEMO=1 (memory store only, no LLM)
  static/        the operator dashboard (single page, no build step)

agents/fleet/    ADK entrypoint, so `adk run` / `adk web` can drive the same fleet
scripts/         simulate.py, the eval harness that writes evidence/summary.json
tests/           test_core.py: metrics, verifier and policy, no credentials needed
probe/           the interpretability probe (separate deps, see probe/RESULTS.md)
evidence/        committed output of the last eval sweep (summary.json, runs.jsonl)
docs/            ARCHITECTURE.md, RESEARCH.md, img/architecture.jpg
```

### Runs in the background, at volume

`POST /tickets` returns immediately and processes asynchronously; `POST /tickets/batch`
enqueues a whole workload of tickets that stream onto the dashboard as they finish. And
`POST /audit` is framework-agnostic: point a list of *any* agent's run logs at it and get
the silent-failure report back, no agents re-run, scaling to large volumes.

```bash
curl -s localhost:8080/audit -H 'content-type: application/json' -d '[
  {"claimed_done": true,  "confidence": 0.95, "verified": true},
  {"claimed_done": true,  "confidence": 0.70, "verified": false},
  {"outcome": "failed",   "confidence": 0.20, "verified": true}
]' | jq '{silent_failure_rate, escalation}'
```

## Eval harness

```bash
uv run python scripts/simulate.py --n 24 --fault-rate 0.3
```

Runs synthetic tickets with planted traps (two customers with the same name, refunds
above the approval limit, an already-cancelled subscription) against tools that
sometimes return *success* without changing the world. Prints reported vs verified
success, silent-failure rate, calibration (Brier/ECE), the risk-coverage curve and the
recommended escalation threshold. Results land in `evidence/summary.json`.

### Results of the last sweep

Dated 2026-08-25, 35% injected tool-fault rate, run on the `gemini-3.5-flash-lite` tier that
now sits at the bottom of the fallback cascade. Read the sample size first:

**10 tickets were dispatched and 4 of them produced no results at all.** The free-tier
15 req/min cap and 503 demand-shedding truncated those four before any worker reported, so
they are not agent failures and they contribute nothing to the rates below. Everything in
the table is computed over the **n = 5** verifiable (claim, verification) pairs that did
complete. Four of those five were claimed done, and one of the four was a silent failure.

| Metric | Value (n = 5) | Reading |
|---|---|---|
| Tickets dispatched / no result | 10 / **4** | truncated by rate limit and 503, not by the fleet |
| Reported success rate | **0.80** | 4 of 5 pairs, on the agents' own scoreboard |
| Verified success rate | **0.60** | 3 of 5 held in the system of record |
| **Silent-failure rate** | **0.25** | 1 of the 4 "done" claims was false, and invisible to the agents |
| False-alarm rate | 0.00 | no run cried failure that was actually fine |
| Brier / ECE | **0.1806** / **0.184** | calibration of the claim, over 5 pairs |
| Escalation threshold | **0.99** | auto-accept 3 of the 4 "done" claims at **0%** residual silent-failure risk; escalate the rest to a human |
| Verifier blind spots | **0** | the runtime verifier's verdict matched hidden ground truth on every completed run |

The gap between reported (0.80) and verified (0.60) success is the whole point: without
Attest the fleet reports the higher number and ships the difference. At n = 5 that is a
working demonstration that the measurement runs end to end, not a benchmark result, and
none of these rates should be read as a population estimate. A larger sweep needs billing
enabled on the GCP project to clear the free-tier cap. `evidence/summary.json` is committed
and holds the authoritative numbers, including `tickets_dispatched` and
`tickets_with_no_result`.

An earlier version of this table reported Brier/ECE as 0.38 / 0.38 and read it as the
agents being badly over-confident. That was a measurement bug, not a finding: `confidence`
is the worker's confidence in **its own claim**, so it has to be mapped through the claim
direction before it is scored against `verified`. A confident "failed" claim that verifies
as failed is well calibrated, and the old code counted it as the opposite. Corrected,
calibration is unremarkable at this sample size and the over-confidence reading does not
survive.

## Deploy

```bash
gcloud auth login
PROJECT=<id> GOOGLE_API_KEY=<key> ./deploy.sh
```

`deploy.sh` enables Cloud Run + Firestore, stores the Gemini key in Secret Manager and
deploys from source. Point a Pub/Sub push subscription or any webhook at `POST /tickets`.

This is the script that produced the live service at
**https://attest-fleet-434066362046.asia-south1.run.app** (region `asia-south1`, Firestore
database `attest-fleet`, `ATTEST_STORE=firestore`). `curl <URL>/health` returns the store
backend and the model ids the running revision actually resolved.

## The eight-step loop, mapped to code

| Step | Where |
|---|---|
| 1 Task input (real trigger) | `POST /tickets` — webhook or Pub/Sub envelope; an attached image is read by **Gemini vision** first (`vision.py`) |
| 2 Decomposition | `fleet_controller`, `agents.py` → `Plan` |
| 3 Context passing | `fleet.py` passes each `Task` alone to its worker |
| 4 Tool calling | `tools.py` (FunctionTools over Firestore) |
| 5 Result verification | `verifier.py` — post-conditions on the system of record |
| 6 Evidence capture | `policy.after_tool` + `fleet._log` → `events`, `runs` |
| 7 Approval / rollback | `policy.before_tool` — **deterministic pre-execution gate** (block state-inconsistent writes), kill switch, approval gate; `/approvals/{id}/approve/rerun` |
| 8 Experience capture | `experience.py` → `playbook` → worker instruction |

## Agent identity list

Served live at `/fleet/identities`. See `agents.py` docstring.

## Failure-mode table

| Failure | Detected by | Fallback |
|---|---|---|
| Tool reports success, state is `pending_gateway` / `cancel_requested` / draft write | verifier post-condition | run marked `silent_failure`; lesson added to playbook |
| State-inconsistent write (refund > balance, act on a non-existent entity) | **pre-execution gate** | blocked before the write; worker reports `failed` |
| Ambiguous customer (two "Priya Sharma") | controller must disambiguate; verifier + eval ground truth | task type `other`, no mutation |
| High-risk action (refund > limit, deletion) | policy gate before the tool runs | approval doc, worker reports `blocked`, operator approves + reruns |
| Worker claims `done` on a `pending_approval` result | verifier | silent failure recorded, lesson captured |
| Transient tool error (`IAM_TIMEOUT`) | tool result | worker reports `failed`; retry on rerun |
| Model rate limit / malformed output | `run_agent` retries, schema validation | run `failed` with error, evidence retained |
| Operator loses trust | kill switch | all mutating tools blocked fleet-wide |

## Grounding in current research

Attest Fleet's design choices are the same conclusions recent agent-reliability papers
reached. See [docs/RESEARCH.md](docs/RESEARCH.md) for the mapping; in short:

- **Independent state verification** beats self-report — false success is 45-48% of failures in
  single-control domains against ~3% in a dual-control one, and cheap deterministic detectors
  beat LLM judges 4–8×
  (Advani, [2606.09863](https://arxiv.org/abs/2606.09863)). Attest verifies against the
  system of record, deterministic-first.
- **Deterministic pre-execution gates** recover silent policy-violations (+12pp; Reddy et
  al., [2607.07405](https://arxiv.org/abs/2607.07405)). Implemented in `policy.py`.
- **Trajectory-level calibration** (ECE) is what agents need (Zhang et al.,
  [2601.15778](https://arxiv.org/abs/2601.15778)); Attest reports Brier/ECE over runs.
- **End-state correctness + injected faults** (Gupta, ReliabilityBench,
  [2601.06112](https://arxiv.org/abs/2601.06112)); the eval harness injects tool faults.
- **Cascaded selective escalation** with a coverage/risk guarantee (Jung, Brahman, Choi,
  ICLR 2025, [2407.18370](https://arxiv.org/abs/2407.18370)); the escalation threshold is
  that operating point.

## Prior work (disclosure)

**No code from any prior project was incorporated.** Every line in this repository was
written for this hackathon: the fleet, the tools, the policy gate, the Firestore store, the
verifier, the metrics module, the dashboard and the eval harness.

The metrics themselves are standard published statistics, not anyone's proprietary work.
Brier score, expected calibration error, the risk-coverage curve and selective escalation
are textbook definitions, implemented fresh in Python here (`src/attest_fleet/metrics.py`).
For completeness: the author worked on the same problem before, in an unrelated TypeScript
project for a different competition, which is why the framing was ready on day one. That
project contributed familiarity with the problem, and nothing else.

## License

Apache-2.0
