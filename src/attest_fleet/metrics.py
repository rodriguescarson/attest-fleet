"""Attest's core arithmetic: the gap between what agents claim and what is true.

Inputs are (claim, verification) pairs. No LLM anywhere in this file.
Reimplemented fresh for this project; the metric definitions come from the
author's earlier Attest work (see README, "Prior work")."""

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


def brier(samples: list[Sample]) -> Optional[float]:
    xs = [(s.confidence - (1.0 if s.verified else 0.0)) ** 2 for s in samples if s.verified is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def ece(samples: list[Sample], bins: int = 10) -> Optional[float]:
    xs = [s for s in samples if s.verified is not None]
    if not xs:
        return None
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        bucket = [s for s in xs if (lo <= s.confidence < hi) or (b == bins - 1 and s.confidence == 1.0)]
        if not bucket:
            continue
        acc = sum(1.0 for s in bucket if s.verified) / len(bucket)
        conf = sum(s.confidence for s in bucket) / len(bucket)
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


def compute(pairs: Iterable[tuple[Claim, Verification]], target_risk: float = 0.02) -> dict:
    samples = to_samples(pairs)
    n = len(samples)
    verifiable = [s for s in samples if s.verified is not None]
    claimed_done = [s for s in verifiable if s.claimed_done]
    claimed_not = [s for s in verifiable if not s.claimed_done]
    silent = [s for s in claimed_done if not s.verified]
    false_alarm = [s for s in claimed_not if s.verified]
    return {
        "n_tasks": n,
        "n_verifiable": len(verifiable),
        "reported_success_rate": _rate(sum(1 for s in samples if s.claimed_done), n),
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
