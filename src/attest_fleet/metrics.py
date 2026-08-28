"""Attest's core arithmetic: the gap between what agents claim and what is true.

Inputs are (claim, verification) pairs. No LLM anywhere in this file.

Brier score, ECE, risk-coverage and selective escalation are standard published
definitions; this is a fresh implementation of them, with no code carried over from
any earlier project (see README, "Prior work")."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from .domain import Claim, Verification


@dataclass
class Sample:
    claimed_done: bool
    confidence: float
    verified: Optional[bool]  # None = unverifiable, excluded from rates


def to_samples(pairs: Iterable[tuple[Claim, Verification]]) -> list[Sample]:
    return [Sample(c.outcome == "done", float(c.confidence), v.verified) for c, v in pairs]


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def p_verified(s: Sample) -> float:
    """The agent's confidence is confidence *in its own claim*, not P(verified=True).

    A worker that reports "blocked" with confidence 1.0 is asserting the task did NOT
    complete, so its implied P(verified) is 0.0, not 1.0. Scoring the raw confidence
    against `verified` charges a correctly-reported failure the maximum penalty and
    inflates the measured over-confidence. Map through the claim direction first."""
    return s.confidence if s.claimed_done else 1.0 - s.confidence


def brier(samples: list[Sample]) -> Optional[float]:
    xs = [(p_verified(s) - (1.0 if s.verified else 0.0)) ** 2 for s in samples if s.verified is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def ece(samples: list[Sample], bins: int = 10) -> Optional[float]:
    xs = [s for s in samples if s.verified is not None]
    if not xs:
        return None
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [s for s in xs if (lo <= p_verified(s) < hi) or (b == bins - 1 and p_verified(s) == 1.0)]
        if not bucket:
            continue
        acc = sum(1.0 for s in bucket if s.verified) / len(bucket)
        conf = sum(p_verified(s) for s in bucket) / len(bucket)
        total += abs(acc - conf) * len(bucket) / len(xs)
    return round(total, 4)


def risk_coverage(samples: list[Sample]) -> list[dict]:
    """For each confidence threshold: what fraction of claimed-done runs we auto-accept
    (coverage) and what fraction of those are silently wrong (risk)."""
    done = sorted([s for s in samples if s.claimed_done and s.verified is not None], key=lambda s: -s.confidence)
    out = []
    if not done:
        return out
    thresholds = sorted({round(s.confidence, 2) for s in done}, reverse=True)
    for t in thresholds:
        accepted = [s for s in done if s.confidence >= t]
        wrong = sum(1 for s in accepted if not s.verified)
        out.append({"threshold": t, "coverage": round(len(accepted) / len(done), 4), "risk": round(wrong / len(accepted), 4), "n": len(accepted)})
    return out


def escalation_threshold(samples: list[Sample], target_risk: float) -> Optional[dict]:
    """Lowest threshold whose residual silent-failure rate is within target."""
    curve = risk_coverage(samples)
    ok = [p for p in curve if p["risk"] <= target_risk]
    if not ok:
        return None
    best = max(ok, key=lambda p: p["coverage"])
    return {**best, "target_risk": target_risk}


def compute_records(records: Iterable[dict], target_risk: float = 0.02) -> dict:
    """Score a batch of generic agent-run logs (framework-agnostic). Each record:
    {claimed_done|outcome, confidence, verified}. No agents re-run — pure measurement,
    so it scales to tens of thousands of logs in one pass."""
    samples = []
    for r in records:
        done = r.get("claimed_done")
        if done is None:
            done = r.get("outcome") == "done"
        v = r.get("verified")
        samples.append(Sample(bool(done), float(r.get("confidence", 0) or 0), v))
    return _report(samples, target_risk)


def compute(pairs: Iterable[tuple[Claim, Verification]], target_risk: float = 0.02) -> dict:
    return _report(to_samples(pairs), target_risk)


def _report(samples: list[Sample], target_risk: float = 0.02) -> dict:
    n = len(samples)
    verifiable = [s for s in samples if s.verified is not None]
    claimed_done = [s for s in verifiable if s.claimed_done]
    claimed_not = [s for s in verifiable if not s.claimed_done]
    silent = [s for s in claimed_done if not s.verified]
    false_alarm = [s for s in claimed_not if s.verified]
    return {
        "n_tasks": n,
        "n_verifiable": len(verifiable),
        # Both success rates are computed over the SAME denominator (verifiable tasks),
        # so the reported-vs-verified gap is a like-for-like comparison.
        "reported_success_rate": _rate(len(claimed_done), len(verifiable)),
        "verified_success_rate": _rate(sum(1 for s in verifiable if s.verified), len(verifiable)),
        "silent_failure_rate": _rate(len(silent), len(claimed_done)),
        "silent_failures": len(silent),
        "false_alarm_rate": _rate(len(false_alarm), len(claimed_not)),
        "false_alarms": len(false_alarm),
        "brier": brier(samples),
        "ece": ece(samples),
        "risk_coverage": risk_coverage(samples),
        "escalation": escalation_threshold(samples, target_risk),
    }
