# Grounding in current research

Attest Fleet was not designed in a vacuum. Its core choices — verify against the system
of record, prefer deterministic checks over an LLM judge, gate risky actions before they
run, and escalate on a measured risk-coverage curve — are the same conclusions a wave of
2025–2026 papers reached independently. Where the literature reports a finding, Attest
Fleet is the running production implementation of it.

All papers below were read and their numbers quoted verbatim (verified 2026-08-25).

---

### 1. Silent / false success is the dominant, hidden failure mode

**Advani, "From Confident Closing to Silent Failure: Characterizing False Success in LLM
Agents"** — arXiv [2606.09863](https://arxiv.org/abs/2606.09863).

- On **τ²-bench**, "false success" (the agent claims done, the environment disagrees) is
  **45–48% of all failures** in single-control domains — but only **3%** in the
  dual-control telecom domain, where an *independent* user-simulator can verify state.
- On **AppWorld**, false success is **75.8%** of failures.
- Critically: **LLM judges reach only 0.65 AUROC on τ²-bench and 0.54 on AppWorld**
  (barely above chance), while **lightweight deterministic detectors hit 0.83 / 0.95,
  recovering 4–8× more false successes at ~3,300× the speed.**

**How Attest implements it.** This is the whole thesis. Attest treats every worker "done"
as a claim and re-checks it against the system of record — the "dual-control" independent
verifier that drops false success from ~48% to ~3%. And it puts **deterministic
post-conditions first** (`verifier.py`), using the LLM auditor only where no deterministic
check exists — exactly the ordering the AUROC numbers argue for. The dashboard's headline
"silent-failure rate" is this paper's central metric, measured live.

### 2. Gate risky actions *before* they run, deterministically

**Reddy, Challaram, Basu, "Reason Less, Verify More: Deterministic Gates Recover a Silent
Policy-Violation Failure Mode in Tool-Using LLM Agents"** — arXiv
[2607.07405](https://arxiv.org/abs/2607.07405).

- **78%** of a budget agent's failures were "silent wrong-state failures" with **no tool
  error**.
- Deterministic, read-only **pre-execution gates** that inspect the proposed call against
  current state before allowing a write lift success **+12.4pp** (29.6→42.0 on gpt-4o-mini;
  replicated +12.3pp), and **+10.4pp** on a frontier model.

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
reliability curve — not a single-turn score. The evidence trail (per-run tool events) is
the trajectory-level signal this paper argues calibration must use. _Roadmap:_ fold
process features (read-back performed? tool error seen?) into the confidence estimate.

### 4. Judge correctness by end-state, and test under injected faults

**Gupta, "ReliabilityBench: Evaluating LLM Agent Reliability Under Production-Like Stress
Conditions"** — arXiv [2601.06112](https://arxiv.org/abs/2601.06112).

- Correctness is decided by **"action metamorphic relations … end-state equivalence rather
  than text matching."** Reliability is measured under **fault tolerance (λ): controlled
  tool/API failures** (chaos engineering), alongside consistency (pass^k) and robustness (ε).

**How Attest implements it.** The verifier decides success by **end-state on the system of
record**, never by matching the agent's text. The eval harness (`scripts/simulate.py`)
**injects tool faults** (a gateway that accepts a refund then goes silent, an address write
that lands in a draft field) — the λ dimension — and scores the verifier against hidden
ground truth. _Roadmap:_ add pass^k consistency and ε-perturbation runs to the harness.

### 5. Escalate on a measured risk-coverage guarantee

**Kim, Suk, Longpre, et al., "Trust or Escalate: LLM Judges with Provable Guarantees for
Human Agreement"** — ICLR 2025, arXiv [2407.18370](https://arxiv.org/abs/2407.18370).

- **Cascaded selective evaluation**: use a cheap judge, **escalate to a stronger judge (or
  a human) only when confidence is low**, with a provable coverage/agreement guarantee.

**How Attest implements it.** Verification is a **cascade**: deterministic post-condition →
(fallback) Gemma auditor → **human escalation**. The **escalation threshold** the dashboard
reports *is* the selective-prediction operating point — it picks the lowest confidence at
which the residual silent-failure rate stays under target, and states the coverage bought.
The human-approval gate is the escalation destination for high-risk actions.

---

## In one line

Independent state verification, deterministic-first detection, pre-execution gates,
end-state correctness, and risk-coverage escalation are, per the 2025–2026 literature, the
things that actually work on agent reliability. Attest Fleet is a running system that does
all five, on Gemini 3.5 + ADK + Cloud Run, and **measures** the result.


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
claimed outcome match the live system of record?** It composes on top of the Google stack
(Model Armor guards the input → ADK agents act on Cloud Run → **Attest verifies against
Firestore** → OTel / Vertex eval observe), rather than replacing any of it.

**Honest novelty.** Independent state verification is not new in distributed systems — it is
post-conditions, integration tests, reconciliation, the saga/outbox pattern. The novelty is
that agent frameworks report success from *self-report*, so the industry dropped the one check
every other critical system keeps. Attest re-introduces it as a **live, calibrated, per-action
governance metric** (silent-failure rate + Brier/ECE + escalation threshold + pre-execution
gates). We did not invent verification; we made it the measurement-and-governance layer for
agents. No productized competitor was found doing this at 2026-08-26.

_Adjacent 2026 research (detection via trace anomalies, not state verification): Pathak et al.
arXiv 2511.04032; the NeurIPS 2026 "Who Verifies the Agents?" workshop._
