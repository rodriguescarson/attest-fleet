"""Model Armor: inline guardrail on untrusted ticket text.

A ticket body is attacker-controlled text that goes straight into a controller prompt, so
it is screened for prompt injection and jailbreak attempts before any agent sees it. This
is Google's Model Armor service, not a regex of our own.

It sits at the edge, deliberately: Attest's own contribution is runtime verification of
outcomes, and input screening is a job the platform already does better. The two compose.

Fails OPEN by design. If the guard is unreachable, a ticket still runs — because the
verifier, the pre-execution state gate and the approval gate all still apply downstream,
and silently dropping real customer tickets because a screening API blipped is the worse
failure. Every decision, including a guard error, is written to the evidence trail.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from . import config

_MAX_CHARS = 1800  # the injection filter caps at ~512 tokens; screen the leading slice


def _endpoint() -> Optional[str]:
    if not (config.MODEL_ARMOR_TEMPLATE and config.VERTEX_PROJECT):
        return None
    loc = config.MODEL_ARMOR_LOCATION
    return (f"https://modelarmor.{loc}.rep.googleapis.com/v1/projects/{config.VERTEX_PROJECT}"
            f"/locations/{loc}/templates/{config.MODEL_ARMOR_TEMPLATE}:sanitizeUserPrompt")


def screen(text: str) -> dict:
    """Return {"checked": bool, "blocked": bool, "reason": str, "confidence": str|None}."""
    url = _endpoint()
    if not url or not (text or "").strip():
        return {"checked": False, "blocked": False, "reason": "guard not configured", "confidence": None}
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps({"userPromptData": {"text": text[:_MAX_CHARS]}}).encode(),
            headers={"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - fixed Google endpoint
            result = json.load(r).get("sanitizationResult", {})
    except Exception as e:  # noqa: BLE001 - fail open, but say so
        return {"checked": False, "blocked": False, "reason": f"guard unavailable: {str(e)[:120]}", "confidence": None}

    pi = (result.get("filterResults", {}).get("pi_and_jailbreak", {}) or {}).get("piAndJailbreakFilterResult", {})
    blocked = result.get("filterMatchState") == "MATCH_FOUND"
    return {
        "checked": True,
        "blocked": blocked,
        "reason": "prompt injection or jailbreak detected" if blocked else "clean",
        "confidence": pi.get("confidenceLevel"),
    }
