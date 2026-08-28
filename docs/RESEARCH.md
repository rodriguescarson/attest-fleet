# Grounding in current research

Attest Fleet was not designed in a vacuum. Its core choices — verify against the system
of record, prefer deterministic checks over an LLM judge, gate risky actions before they
run, and escalate on a measured risk-coverage curve — are the same conclusions a wave of
2025–2026 papers reached independently. Where the literature reports a finding, Attest
Fleet is the running production implementation of it.

Every quoted number below was taken from the paper it is attributed to. The bylines were
not checked with the same care: the entry for arXiv 2407.18370 originally carried the author
list of a different paper (Prometheus 2, arXiv 2405.01535) and has been corrected. Treat the
figures as verified against the source and the attributions as best-effort, and re-check any
byline before quoting it elsewhere.

---

### 1. Silent / false success is the dominant, hidden failure mode

**Advani, "From Confident Closing to Silent Failure: Characterizing False Success in LLM
Agents"** — arXiv [2606.09863](https://arxiv.org/abs/2606.09863), accepted to FAGEN@ICML 2026.

- On **τ²-bench**, "false success" (the agent claims done, the environment disagrees) is
  **45–48% of all failures** in single-control domains — but only **3%** in the
  dual-control telecom domain, where an *independent* user-simulator can verify state.
- On **AppWorld**, false success reaches **75.8%** among self-assessing coding-agent
  trajectories that make an explicit status claim (a qualified subset, not all failures).
- Critically: **LLM judges reach only 0.65 AUROC on τ²-bench and 0.54 on AppWorld**
  (barely above chance), while **lightweight deterministic detectors hit 0.83 / 0.95,
  recovering 4–8× more false successes at ~3,300× the speed.**

**How Attest implements it.** This is the whole thesis. Attest treats every worker "done"
as a claim and re-checks it against the system of record — the "dual-control" independent
verifier: false success is 45-48% in single-control domains versus ~3% in the dual-control
telecom domain — a cross-domain comparison, not a measured intervention. And it puts **deterministic
post-conditions first** (`verifier.py`), using the LLM auditor only where no deterministic
check exists — exactly the ordering the AUROC numbers argue for. The dashboard's headline
"silent-failure rate" is this paper's central metric, measured live.

**What we measure.** Over a 40-ticket sweep at a 30% injected tool-fault rate on
`gemini-3.7-flash` (Vertex AI), the silent-failure rate is **0.0476**: one of 21 claimed-done
tasks was false. An earlier 5-pair sweep on `gemini-3.5-flash-lite` put it at **0.25**. The
useful reading is not either number on its own but the fact that they differ by a factor of
five: the rate is a property of the model, the tooling and the workload, so it has to be
measured on the system you are actually running, and re-measured when the model under it
changes. It did not reach zero on the stronger model either.

**Where post-condition verification falls short.** The same sweep scores the verifier against
hidden ground truth and reports **2 blind spots in 40 runs**, both on a deliberately ambiguous
ticket: two customers share a name, the controller picks one, the write lands, and the
post-condition ("is the address now the requested value?") passes because it checks that the
action happened rather than that the right entity was chosen. Post-conditions verify actions;
identity resolution needs a check of its own. Reporting that is part of the method, not a
footnote to it.

### 2. Gate risky actions *before* they run, deterministically

**Reddy, Challaram, Basu, "Reason Less, Verify More: Deterministic Gates Recover a Silent
Policy-Violation Failure Mode in Tool-Using LLM Agents"** — arXiv
[2607.07405](https://arxiv.org/abs/2607.07405).

- **78%** of a budget agent's failures were "silent wrong-state failures" with **no tool
  error**.
- Deterministic, read-only **pre-execution gates** that inspect the proposed call against
  current state before allowing a write lift success **+12.4pp** (29.6→42.0 on gpt-4o-mini;
  replicated +12.3pp). The authors also report **+10.4pp** on a frontier model but label it
  "suggestive evidence, not a central claim" (n=5, no replication), so we do not lean on it.

**How Attest implements it.** `policy.py` runs a **pre-execution gate** on every mutating
tool call: it validates the proposed action against current state (a refund cannot exceed
the refundable balance; you cannot act on a customer/order/subscription that does not
exist; a cancel of an already-cancelled sub is a no-op) and blocks inconsistent writes
*before* they happen, logging each gate decision as evidence. This complements the
post-hoc verifier: the gate prevents policy-violation silent failures, the verifier
catches gateway/draft-write silent failures after the fact.

### 3. Calibration must be trajectory-level, not single-turn

**Zhang, Xiong, Wu, "Agentic Confidence Calibration"** — arXiv
[2601.15778](https://arxiv.org/abs/2601.15778).

- "Existing calibration methods, built for static single-turn outputs, cannot address the
  unique challenges of agentic systems, such as compounding errors along trajectories,
  uncertainty from external tools, and opaque failure modes." Proposes Holistic Trajectory
  Calibration; **ECE** is the primary metric.

**How Attest implements it.** Attest computes **Brier and ECE over whole runs**, pairing
each worker's self-reported confidence with the independent verdict, and reports the
reliability curve — not a single-turn score. On the 40-run sweep that is **Brier 0.023, ECE
0.0328**: on this model and workload the claim confidence is well calibrated and usable as a
routing signal. An earlier build of this repo reported the opposite and called the agents badly
over-confident; that was a sign-mapping bug in the metric (confidence is confidence in the
agent's *own claim*, so it must be mapped through the claim direction before scoring), and the
over-confidence reading did not survive the fix. The evidence trail (per-run tool events) is
the trajectory-level signal this paper argues calibration must use. _Roadmap:_ fold
process features (read-back performed? tool error seen?) into the confidence estimate.

### 4. Judge correctness by end-state, and test under injected faults

**Gupta, "ReliabilityBench: Evaluating LLM Agent Reliability Under Production-Like Stress
Conditions"** — arXiv [2601.06112](https://arxiv.org/abs/2601.06112).

- Correctness is decided by **"action metamorphic relations … end-state equivalence rather
  than text similarity."** Reliability is measured under **fault tolerance (λ): controlled
  tool/API failures** (chaos engineering), alongside consistency (pass^k) and robustness (ε).

**How Attest implements it.** The verifier decides success by **end-state on the system of
record**, never by matching the agent's text. The eval harness (`scripts/simulate.py`)
**injects tool faults** (a gateway that accepts a refund then goes silent, an address write
that lands in a draft field) — the λ dimension — and scores the verifier against hidden
ground truth. At λ = 0.3, 11 of 40 runs ended `failed`: the fault made the task genuinely
impossible, the worker said so, and the verifier confirmed it against the store, which is why
the false-alarm rate is 0.00. Honest failure under injected faults is the expected reading of
that row, not a broken fleet. _Roadmap:_ add pass^k consistency and ε-perturbation runs to the harness.

### 5. Escalate on a measured risk-coverage guarantee

**Jung, Brahman, Choi, "Trust or Escalate: LLM Judges with Provable Guarantees for
Human Agreement"** — ICLR 2025, arXiv [2407.18370](https://arxiv.org/abs/2407.18370).

- **Cascaded selective evaluation**: use a cheap judge, **escalate to a stronger judge (or
  a human) only when confidence is low**, with a provable coverage/agreement guarantee.

**How Attest implements it.** Verification is a **cascade**: deterministic post-condition →
(fallback) Gemma auditor → **human escalation**. The **escalation threshold** the dashboard
reports *is* the selective-prediction operating point — it picks the lowest confidence at
which the residual silent-failure rate stays under target, and states the coverage bought.
On the 40-run sweep, against a target residual risk of 0.02, that point is **0.98: 95.2%
coverage at 0% residual silent-failure risk**, so 20 of 21 done claims auto-accept and the
rest go to a human. The human-approval gate is the escalation destination for high-risk
actions.

---

## In one line

Independent state verification, deterministic-first detection, pre-execution gates,
end-state correctness, and risk-coverage escalation are, per the 2025–2026 literature, the
things that actually work on agent reliability. Attest Fleet is a running system that does
all five, on Gemini 3.7 (Vertex AI) + ADK + Cloud Run, and **measures** the result: 40 runs at
a 30% injected fault rate, silent-failure rate 0.0476, Brier 0.023, ECE 0.0328, 2 verifier
blind spots reported alongside.


## Where Attest Fleet fits the field (composes, not competes)

The agent-observability field is crowded, but it measures the wrong thing for this failure
mode. LangSmith, Arize Phoenix, Braintrust, Galileo, W&B Weave, Langfuse, and DeepEval all
observe the agent's own output: traces, transcripts, or an **LLM-as-judge**. Advani's finding
above is the point — an LLM judge catches false success at **AUROC ≤ 0.65** (barely above
chance), because a confident wrong claim reads as success to another model too. Google's own
tools sit at the edges of the problem, not the center:

| Layer | Google tool | What it checks | The gap |
|---|---|---|---|
| Input guard | **Model Armor** | prompt injection, jailbreak, PII, malicious URLs | screens content, not outcomes |
| Offline eval | **Vertex Gen AI Evaluation** | rubric metrics vs a golden dataset | offline, against references, not the live record |
| Telemetry | **OpenTelemetry GenAI** | agent / tool / memory spans | records calls; defines no notion of a verified outcome |

**Attest Fleet adds the layer none of them cover: at runtime, in production, does the agent's
claimed outcome match the live system of record?** It sits on top of the Google stack rather
than replacing any of it, and most of that stack is wired in rather than cited: Model Armor
screens the ticket text at ingress (`guard.py`, fail-open, every decision logged) → ADK agents
act on Cloud Run with Gemini on Vertex AI → **Attest verifies against Firestore** → ADK's OTel
GenAI spans export to Cloud Trace.

**Honest novelty.** Independent state verification is not new in distributed systems — it is
post-conditions, integration tests, reconciliation, the saga/outbox pattern. The novelty is
that agent frameworks report success from *self-report*, so the industry dropped the one check
every other critical system keeps. Attest re-introduces it as a **live, calibrated, per-action
governance metric** (silent-failure rate + Brier/ECE + escalation threshold + pre-execution
gates). We did not invent verification; we made it the measurement-and-governance layer for
agents. No productized competitor was found doing this at 2026-08-26.

_Adjacent 2026 research (detection via trace anomalies, not state verification): Pathak et al.
arXiv 2511.04032; the NeurIPS 2026 "Who Verifies the Agents?" workshop._


## Measured on our own data: the representation-action gap

We did not just cite the interpretability angle; we ran it. Using the linear-probe recipe
of Paper 17 (*Reading the Lie Factor*), we trained a probe on an open model's activations
to predict silent failure on 400 surface-matched auditor inputs, against three controls
(full method + reproduction in [`probe/RESULTS.md`](../probe/RESULTS.md)):

| 5-fold CV AUROC | value |
|---|---|
| Linear probe on activations | **1.000** |
| TF-IDF surface baseline | 0.533 |
| Stated confidence (self-report) | 0.538 |
| Shuffled-label null | 0.471 |

The probe row is a best-of-layers number, picked by the same cross-validated AUROC it
reports, and the shuffled null is computed only at that already-chosen layer, so it does not
get the same selection advantage. See [`probe/RESULTS.md`](../probe/RESULTS.md) for what
that does and does not license.

The task is surface-controlled (`$490.00` requested vs `490.0` read back — same value,
different tokens), so bag-of-words collapses to chance (0.53) and the agent's own
confidence is at chance (0.54) — yet the model's **internal activations linearly separate
silent failures at 1.00**. The information to catch the failure is present in what the
model *represents* and absent from what it *says*. That is the mechanistic case for
Attest: read the state (deterministic verifier, shipping today) or the internals (probe,
the white-box roadmap tier), never the self-report. AUROC 1.00 is on a controlled
synthetic discrepancy — the point is the contrast, not the number; the probe is validated
against state verification as ground truth, which is why verification stays primary.
