"""Multimodal intake: read an image a customer attached to a support ticket.

A real support ticket often arrives with a screenshot — an error dialog, a receipt,
a photo of a damaged product. The vision reader turns that into text the controller
can plan on, before any action is taken. Google Gemini (multimodal) does the read."""

from __future__ import annotations

import asyncio
import base64
import os
import ipaddress
import socket
import urllib.parse
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


MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _assert_fetchable(url: str) -> None:
    """A ticket attachment is attacker-controlled input: the ticket endpoint accepts any
    URL, and this service runs on Cloud Run next to a metadata server. Restrict fetches to
    public http(s) hosts so a ticket cannot make the fleet read link-local or private
    addresses (169.254.169.254 and friends)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported attachment scheme {parsed.scheme!r}")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("attachment url has no host")
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve attachment host {host!r}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"attachment host {host!r} resolves to non-public address {ip}")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects on attachment fetches.

    _assert_fetchable resolves the host and rejects private addresses, but urlopen then
    re-resolves and follows redirects without re-checking, so a public host could 302 to
    169.254.169.254. Refusing redirects closes both that and the DNS-rebinding window."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError(f"attachment redirected to {newurl!r}; refusing to follow")


_OPENER = urllib.request.build_opener(_NoRedirect)


def _load_image(url: str) -> tuple[bytes, str]:
    if url.startswith("data:"):
        head, b64 = url.split(",", 1)
        mime = head[5:].split(";")[0] or "image/png"
        return base64.b64decode(b64), mime
    _assert_fetchable(url)
    req = urllib.request.Request(url, headers={"User-Agent": "attest-fleet/0.1"})
    with _OPENER.open(req, timeout=15) as r:  # noqa: S310 — validated by _assert_fetchable, redirects refused
        data = r.read(MAX_IMAGE_BYTES + 1)
        mime = (r.headers.get("Content-Type") or "image/png").split(";")[0]
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"attachment exceeds {MAX_IMAGE_BYTES} bytes")
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
