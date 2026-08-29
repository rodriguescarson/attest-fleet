"""Voice intake: a support call becomes a verified fleet run.

Phone is still the dominant channel in customer operations, and a voicemail or a call
recording is the one input a text-only agent fleet cannot touch. This transcribes the
audio and pulls out what the fleet actually needs to plan on, before the controller sees
anything, so a spoken ticket runs through exactly the same policy gate, the same workers
and the same verifier as a typed one.

The point is not that the fleet can hear. It is that a claim originating from speech is
verified against the system of record on identical terms to a claim originating from a
web form. Modality changes the intake; it must not change the standard of proof.

Gemini 3.7 accepts audio natively on Vertex, so this is the model the fleet already runs
rather than a bolted-on speech service.
"""

from __future__ import annotations

import asyncio
import base64
import urllib.request

from google import genai
from google.genai import types

from . import config
from .vision import MAX_IMAGE_BYTES, _OPENER, _assert_fetchable

MAX_AUDIO_BYTES = 20 * 1024 * 1024  # a few minutes of speech

PROMPT = (
    "This is a recorded customer support call or voicemail. In 2 to 4 factual sentences, "
    "state what the customer is asking for. Quote any order id, amount, email address, "
    "postal address or account detail exactly as spoken. If the caller makes more than one "
    "request, list each one. Do not infer anything that was not said, and do not offer to "
    "help: this is a transcription-and-extraction step, not a reply to the customer."
)


def _load_audio(url: str) -> tuple[bytes, str]:
    if url.startswith("data:"):
        head, b64 = url.split(",", 1)
        mime = head[5:].split(";")[0] or "audio/wav"
        return base64.b64decode(b64), mime
    _assert_fetchable(url)  # same SSRF guard as image attachments: public hosts only
    req = urllib.request.Request(url, headers={"User-Agent": "attest-fleet/0.1"})
    with _OPENER.open(req, timeout=20) as r:  # noqa: S310 - validated, redirects refused
        data = r.read(MAX_AUDIO_BYTES + 1)
        mime = (r.headers.get("Content-Type") or "audio/wav").split(";")[0]
    if len(data) > MAX_AUDIO_BYTES:
        raise ValueError(f"call recording exceeds {MAX_AUDIO_BYTES} bytes")
    return data, mime


def _read_sync(url: str) -> str:
    data, mime = _load_audio(url)
    client = genai.Client(**config.client_kwargs_for(config.VOICE_MODEL))
    resp = client.models.generate_content(
        model=config.VOICE_MODEL,
        contents=[types.Part.from_bytes(data=data, mime_type=mime), PROMPT],
    )
    return (resp.text or "").strip()


async def read_call(url: str) -> str:
    """Transcribe and summarise a call recording. Raises on a bad attachment; the caller
    logs it and continues, because a broken recording must not sink the ticket."""
    return await asyncio.to_thread(_read_sync, url)
