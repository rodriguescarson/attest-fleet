# What Attest Fleet actually is

A plain-language walkthrough of the deployed system, written so a reader who has never seen
the code can follow it, and so a reviewer can find the places where we made a judgement call.

Live: **https://attest-fleet-434066362046.asia-south1.run.app**

---

## The one-sentence version

Every AI agent framework reports a success rate. Almost none of them *check* it. The agent
finishes a job, says "done", and that self-report becomes the number on the dashboard.

Attest Fleet treats every "done" as a **claim**, then reads the actual database to see
whether the world agrees. When it doesn't, that is a **silent failure**: the agent believes
it succeeded, the dashboard is green, and nobody finds out until a customer complains.

**A real one, from the eval run that is on the live board right now:**

> An agent reported it had cancelled a customer's subscription. Confidence 0.95. Its own
> evidence line reads `get_subscription returned subscription sub_9003 with
> status='cancel_requested'`. The subscription was never cancelled — it was only *requested*.
> The post-condition check failed, and Attest labelled the run a silent failure.

The agent was not lying. It could not tell. That is the entire problem.

---

## Why this is worth building

Recent work on agent reliability found that "false success" — the agent claims done, the
environment disagrees — accounts for **45 to 48% of all failures** in single-control
benchmark domains, against about 3% in a dual-control domain where an independent verifier
checks state (Advani, [arXiv 2606.09863](https://arxiv.org/abs/2606.09863), accepted to
FAGEN@ICML 2026).

The same paper found that using another LLM as a judge catches this at **AUROC ≤ 0.65** —
barely above chance — while cheap deterministic checks reach 0.83 to 0.95. That matters:
it means the whole category of agent-observability tooling that grades agents with an AI
judge is structurally blind to this exact failure. A confident wrong answer looks like
success to another model too.

Attest reads the record instead.

---

## How one ticket flows through

1. **A ticket arrives** from a webhook or a Pub/Sub push. Not a chat box — this is a
   background system.
2. **Model Armor screens it.** Ticket text is untrusted input, so Google's Model Armor
   checks it for prompt injection and jailbreak before any agent reads a word.
3. **If a photo is attached, a vision model reads it** into text first.
4. **The controller resolves the customer and splits the ticket into typed tasks.** One
   ticket carrying two asks becomes two tasks for two different specialists.
5. **Specialist agents execute.** `billing_agent` handles refunds; `account_agent` handles
   addresses, cancellations, unlocks, deletions. Each sees only its own task, never the
   whole ticket.
6. **Every tool call passes the policy gate first:** fleet kill switch, a deterministic
   check that the action is consistent with current state, a tool-call budget that stops a
   looping worker, and a human-approval hold on high-risk actions.
7. **The verifier reads the system of record** and checks the post-conditions the task
   implies. Deterministic checks first; the Gemma auditor only where no deterministic check
   exists.
8. **Caught failures become lessons** injected into that worker's next run.

---

## Architecture

```
                          ┌──────────────┐
  ticket  ───────────────►│ MODEL ARMOR  │   untrusted text screened first
  (webhook / Pub/Sub)     └──────┬───────┘
                                 ▼
                        ┌────────────────┐
                        │ fleet_controller│  resolves customer, splits into tasks
                        └───┬────────┬────┘
                     ┌──────┘        └──────┐
                     ▼                      ▼
              billing_agent           account_agent      one task each, scoped tools
                     └──────┐        ┌──────┘
                            ▼        ▼
                        ┌─────────────────┐
                        │  POLICY GATE    │  kill switch · state gate
                        │                 │  approval hold · loop budget
                        └────────┬────────┘
                                 ▼
                          ┌─────────────┐
                          │  FIRESTORE  │   the system of record
                          └──────┬──────┘
                                 ▼
   worker's claim ──────►┌────────────────┐
   "done" + confidence   │   VERIFIER     │  deterministic checks first,
   actual state ────────►│                │  Gemma auditor only if needed
                         └───────┬────────┘
                                 ▼
                    verified  ◄──┴──►  SILENT FAILURE
                        │                    │
                        ▼                    ▼
                    metrics            playbook lesson
              reported vs verified   injected into the next run
              Brier / ECE / escalation
```

Google components, named: **Vertex AI** (Gemini 3.7 Flash), **Gemma** (independent
auditor), **Agent Registry**, **Model Armor**, **Cloud Run**, **Firestore**, **Cloud Trace**.

---

## What is genuinely built, and what is not

The Fortified Enterprise Fleet track names seven platform primitives. The honest tally:

| Primitive | Status | What that means here |
|---|---|---|
| Agent Registry | **implemented** | All five agents published to Google's Agent Registry as A2A agent cards, tools indexed as skills. `GET /fleet/identities` reads them back live; it is not a hardcoded list. |
| Agent Runtime | **implemented** | Cloud Run, background execution, model fallback cascade on demand-shedding. |
| Memory Bank | **implemented** | Firestore holds runs, events, approvals and the playbook; lessons persist across runs. |
| Agent Gateway | **implemented** | The policy gate: kill switch, pre-execution state gate, approval hold, loop budget. |
| Model Armor | **implemented** | Ingress screening for prompt injection. Fails open by design. |
| Agent Observability | **implemented** | ADK's OpenTelemetry GenAI spans export to Cloud Trace, plus the operator dashboard. |
| Agent Identity | **partial** | Agents have typed identities and scoped tools, now published to the registry, but every agent still acts under the service's own principal. There is no per-agent credential. We say so rather than claiming it. |

---

## The measurement, and the awkward part

40 tickets, 30% of tool calls deliberately faulted, `gemini-3.7-flash` on Vertex AI, zero
dropped tickets.

| | Value | Reading |
|---|---|---|
| Agents reported success | 0.525 | the self-reported scoreboard |
| Actually succeeded | 0.500 | what the records showed |
| **Silent-failure rate** | **0.048** | 1 of 21 "done" claims was false |
| False alarms | 0.00 | nothing cried failure that was actually fine |
| Brier / ECE | 0.023 / 0.033 | claim confidence is well calibrated here |
| Verifier's own blind spots | 2 of 40 | the harness scores the verifier too |

**An earlier, much smaller run reported 0.25**, and that was the headline for a while. It
came from four claimed-done tasks on `gemini-3.5-flash-lite`. We did not delete it — both
sweeps sit side by side in the README. The finding is:

> The silent-failure rate is a property of the model, not a constant. It fell roughly
> fivefold when the model tier changed, and it did not reach zero. You cannot know your own
> rate without measuring it, and last quarter's number is not today's.

Two caveats we state rather than bury: 21 claimed-done tasks is a small denominator, and the
two sweeps differ in fault rate as well as model, so it is a comparison of two operating
points and not a controlled ablation.

**The 11 `failed` runs are the system working.** With 30% of mutating calls faulted, many
tasks genuinely cannot complete. In all 11 the worker reported `failed` and the verifier
confirmed it against the record, which is why the false-alarm rate is 0.00.

**The two verifier blind spots were both the same ambiguous ticket** — two customers share a
name, the controller picks one, the write lands, and the post-condition passes because it
checks that the action happened rather than that the right entity was chosen. Post-conditions
verify actions; identity resolution needs a check of its own. That is a real limitation of
the method and it is in `docs/RESEARCH.md`.

---

## A note on what you cannot demo

Under full fault injection, **Gemini 3.7 reads the record back and honestly reports
`failed`** — verified on both the refund and the address path at 0.95 and 0.99 confidence.
That is our own instruction design working, and it is why the rate is 4.8% rather than 25%.

So a confidently-wrong agent cannot be produced on demand. The one in the opening of this
document is a real run from the eval sweep, not a staged one. `POST /fleet/fault` exposes
fault injection as an operator control so the mechanism can be exercised live, but it
demonstrates that the verifier checks regardless of whether the agent was honest — it does
not manufacture a lie.

---

## Where things live

| | |
|---|---|
| Live app | https://attest-fleet-434066362046.asia-south1.run.app |
| Code | https://github.com/rodriguescarson/attest-fleet |
| Reload the board from real eval evidence | `POST /admin/seed?token=…` |
| Operator token | published in the README, so the project is testable without restriction |
| Zero-credential local run | `ATTEST_DEMO=1 ATTEST_STORE=memory` |

The board is populated from `evidence/runs.jsonl` — the real sweep. Fabricated sample runs
exist in `src/attest_fleet/demo.py` for local UI work and **cannot** be written to a
deployed store; there is a regression test asserting it.
