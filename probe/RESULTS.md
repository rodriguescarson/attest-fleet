# Interpretability probe — do a model's internals encode silent failure?

**Method (Goldowsky-Dill et al. 2025 linear-probe recipe, the setup of Paper 17
"Reading the Lie Factor").** 400 balanced auditor-input examples (TASK / CLAIM / TOOL
EVENTS), label 1 = silent failure (worker claims `done`, the read-back disagrees), 0 =
genuine. Extract mean-pooled residual-stream activations from an open model per layer;
fit an L2 logistic **linear probe**; report 5-fold cross-validated AUROC against three
controls: a shuffled-label null, a **TF-IDF surface baseline**, and the worker's own
**stated confidence**. Model: `Qwen/Qwen2.5-1.5B-Instruct` (open; the Paper-17 family).
A one-line `MODEL_ID` swap runs it on Gemma (Attest's auditor) with an HF token.
Reproduce: `python probe/gen_dataset.py && python probe/run_probe.py`.

## Two datasets, and why the second is the honest one

| Signal (5-fold CV AUROC) | Easy (lexically separable) | **Hard (surface-matched)** |
|---|---|---|
| Linear probe on activations | 1.000 | **1.000** (best layer 5/29) |
| TF-IDF surface baseline | 0.907 | **0.533** |
| Stated confidence (self-report) | 0.531 | **0.538** |
| Shuffled-label null | 0.539 | 0.471 |

The **easy** set leaks: the silent/genuine evidence strings differ lexically, so even
bag-of-words scores 0.91 — a probe hitting 1.0 there proves nothing. The **hard** set is
surface-matched: the requested amount appears as `$490.00` and the read-back as `490.0`
(same value, different tokens), no single token is class-diagnostic, and the label is
purely *does the read-back equal the request*. Bag-of-words **cannot** read that relation,
and TF-IDF duly collapses to **0.533** — while the probe stays at **1.000**.

## The finding

On a task where **surface text (0.53) and the agent's own stated confidence (0.54) are
both at chance**, a **linear probe on the model's activations separates silent failures
at AUROC 1.00**. The information needed to catch the failure is **linearly present in the
model's internal representation** — and **absent from what the agent says**. The gap
between what the model *represents* (1.00) and what it *reports* (0.54) is the
representation-action gap: the mechanistic root of silent failure.

## Why this matters for Attest Fleet (composition, not replacement)

- **Attest reads the world** (deterministic state verification) — ground truth, works on
  any hosted model, catches environment/boundary failures a probe cannot see.
- **A probe reads the model's mind** (this result) — a cheap, inline, white-box signal
  that would feed the confidence estimate driving Brier/ECE calibration and the escalation
  threshold. It is the white-box upgrade to the Gemma-auditor tier.
- Both recover exactly what the confident self-report hides. Attest ships the first today;
  the probe is the roadmap, grounded in the author's Paper 17.

## Honesty / limits

- AUROC 1.00 is on a **controlled synthetic** discrepancy, not a production rate; real
  silent failures are messier. The point is the **contrast** (probe 1.00 vs confidence 0.54
  vs surface 0.53), not the perfection.
- The probe is validated **against state verification as ground truth** — i.e., the world
  labels the probe, which is why state verification stays primary and the probe is a
  complementary tier, never a replacement.
- Controls are the credibility: the easy→hard TF-IDF drop (0.91→0.53) proves the hard set
  is genuinely surface-controlled; the shuffled null (~0.5) proves the probe is not fitting
  noise. Run on Qwen-2.5-1.5B; the Gemma-specific run is a `MODEL_ID` swap.
