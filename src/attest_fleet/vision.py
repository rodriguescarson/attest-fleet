"""Multimodal intake: read an image a customer attached to a support ticket.

A real support ticket often arrives with a screenshot — an error dialog, a receipt,
a photo of a damaged product. The vision reader turns that into text the controller
can plan on, before any action is taken. Google Gemini (multimodal) does the read."""

from __future__ import annotations

import asyncio
import base64
import os
import urllib.request

from google import genai
from google.genai import types

from . import config

_client: genai.Client | None = None


def _client_():
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
    return _client


def _load_image(url: str) -> tuple[bytes, str]:
    if url.startswith("data:"):
        head, b64 = url.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
        return base64.b64decode(b64), mime
    req = urllib.request.Request(url, headers={"User-Agent": "attest-fleet/0.1"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 — operator-supplied ticket attachment
        data = r.read()
        mime = (r.headers.get("Content-Type") or "image/png").split(";")[0]
    return data, mime


PROMPT = (
    "You are a support-desk triage assistant. A customer attached this image to a ticket. "
    "In 1-2 factual sentences, describe what it shows — quote any error message, order id, "
    "amount, or product visible. If it is unreadable, say so. Do not speculate."
)


async def read_attachment(url: str) -> str:
    """Return a short factual description of the ticket's attached image."""
    data, mime = await asyncio.to_thread(_load_image, url)

    def _call() -> str:
        r = _client_().models.generate_content(
            model=config.VISION_MODEL,
            contents=[types.Part.from_bytes(data=data, mime_type=mime), PROMPT],
        )
        return (r.text or "").strip()

    return await asyncio.to_thread(_call)
