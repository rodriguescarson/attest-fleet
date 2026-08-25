# Attest Fleet

[![CI](https://github.com/rodriguescarson/attest-fleet/actions/workflows/ci.yml/badge.svg)](https://github.com/rodriguescarson/attest-fleet/actions/workflows/ci.yml)

**A governed agent fleet whose every claim is independently verified.**
Built for the All Things Agentic Hackathon (Google) — Fortified Enterprise Fleet.

Every agent framework reports success from the agent's own self-report. When a worker
says "refund issued" and the gateway left it `pending_gateway`, the dashboard stays green
and the customer never gets their money. Attest Fleet runs a customer-operations fleet on
Gemini 3.5 + ADK and treats every worker's "done" as a **claim** to be checked against the
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

## Stack (hackathon gate)

| Requirement | Used |
|---|---|
| Gemini 3.5+ | `gemini-3.5-flash-lite` (controller + workers), Gemini vision reads ticket screenshots |
| Google agent framework | Agent Development Kit (`google-adk` 2.7) — `LlmAgent`, tools, `output_schema`, tool callbacks |
| Google Cloud infra | Cloud Run (service), Firestore (system of record + evidence store) |
| Extra Google models (bonus) | **Gemma** (`gemma-4-31b-it`) as the independent auditor; **Gemini vision** for image intake — both motivated, not bolted on |

## Run locally (no cloud needed)

```bash
uv sync
cp .env.example .env            # add GOOGLE_API_KEY (AI Studio) — ATTEST_STORE=memory
uv run pytest                   # metrics, verifier, policy — no LLM calls
uv run uvicorn attest_fleet.web:app --reload --port 8080
```

Send a trigger:

```bash
curl -s localhost:8080/tickets?wait=1 -H 'content-type: application/json' -d '{
  "customer_ref": "Priya Sharma",
  "subject": "Refund last month",
  "body": "Please refund my Pro monthly charge of 49. My email is priya.sharma@example.com."
}' | jq .status
```

Then open http://localhost:8080 for the operator dashboard (metrics, pending approvals,
kill switch, run evidence).

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

Results (10-ticket run, 35% tool-fault rate, `gemini-3.5-flash-lite`, 2026-08-25):

| Metric | Value | Reading |
|---|---|---|
| Reported success rate | **0.80** | what the agents' own scoreboard claims |
| Verified success rate | **0.60** | what actually held in the system of record |
| **Silent-failure rate** | **0.25** | one in four "done" claims was false — invisible to the agents |
| False-alarm rate | 0.00 | no runs cried failure that were actually fine |
| Brier / ECE | 0.38 / 0.38 | the agents are badly over-confident |
| Escalation threshold | **0.99** | auto-accept 75% of claims at **0%** residual silent-failure risk; send the rest to a human |
| Verifier blind spots | **0** | the runtime verifier's verdict matched hidden ground truth on every run |

The 20-point gap between reported and verified success is the whole point: without Attest the
fleet reports 80% and ships the 20%. (Run truncated by the free-tier 15 req/min cap; enable
billing on the GCP project for a full sweep. `evidence/summary.json` holds the raw numbers.)

## Deploy

```bash
gcloud auth login
PROJECT=<id> GOOGLE_API_KEY=<key> ./deploy.sh
```

`deploy.sh` enables Cloud Run + Firestore, stores the Gemini key in Secret Manager and
deploys from source. Point a Pub/Sub push subscription or any webhook at `POST /tickets`.

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

- **Independent state verification** beats self-report — false success drops from ~48% to
  ~3% with a dual-control verifier, and cheap deterministic detectors beat LLM judges 4–8×
  (Advani, [2606.09863](https://arxiv.org/abs/2606.09863)). Attest verifies against the
  system of record, deterministic-first.
- **Deterministic pre-execution gates** recover silent policy-violations (+12pp; Reddy et
  al., [2607.07405](https://arxiv.org/abs/2607.07405)). Implemented in `policy.py`.
- **Trajectory-level calibration** (ECE) is what agents need (Zhang et al.,
  [2601.15778](https://arxiv.org/abs/2601.15778)); Attest reports Brier/ECE over runs.
- **End-state correctness + injected faults** (Gupta, ReliabilityBench,
  [2601.06112](https://arxiv.org/abs/2601.06112)); the eval harness injects tool faults.
- **Cascaded selective escalation** with a coverage/risk guarantee (Kim et al., ICLR 2025,
  [2407.18370](https://arxiv.org/abs/2407.18370)); the escalation threshold is that
  operating point.

## Prior work (disclosure)

The verification metrics (silent-failure rate, Brier/ECE, risk-coverage, escalation
threshold) come from the author's earlier project **Attest** (TypeScript, built for a
different competition). Everything in this repository — the fleet, tools, policy gate,
Firestore store, verifier, harness — is new code written for this hackathon; the metric
definitions were re-implemented in Python from the published definitions.

## License

Apache-2.0
