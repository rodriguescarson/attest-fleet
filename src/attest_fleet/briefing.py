"""The spoken shift briefing.

This fleet is built for a night-shift support lead who inherits agents she did not write.
At 2am she is not reading a dashboard: she is on a phone, walking a floor, or handling the
customer whose refund never arrived. A board that only speaks in pixels is unavailable to
her at exactly the moment it matters most.

So the verification state is also available as speech. Not a screen reader over the DOM,
and not a chat interface: a written briefing whose *content is chosen for listening*. Audio
is linear and unskimmable, so it leads with what would change her next action, names the
one thing that is wrong, and stops. Everything else stays on the screen where scanning is
cheap.

The rule the whole file obeys: speech is a second channel onto the same verified state,
never a second source of truth. The numbers spoken here are the numbers `metrics.py`
computed, and nothing is generated that the board does not also show.

TTS is Developer-API only (Vertex rejects the AUDIO response modality), the same backend
split the Gemma auditor already uses. Output is raw PCM, wrapped here as WAV.
"""

from __future__ import annotations

import asyncio
import io
import struct
import wave
from typing import Any, Optional

from google import genai
from google.genai import types

from . import config

_PCM_RATE = 24000


def compose(m: dict[str, Any], pending: int, kill_switch: bool,
            worst: Optional[dict] = None) -> str:
    """The briefing script, built from measured state only.

    Ordered for the ear: the thing that needs a decision first, the number that frames it
    second, reassurance last. A listener cannot skip ahead, so nothing important goes late.
    """
    runs = m.get("runs") or 0
    sf = m.get("silent_failures") or 0
    rate = m.get("silent_failure_rate")
    verified = (m.get("by_status") or {}).get("verified", 0)

    parts: list[str] = []

    if kill_switch:
        parts.append("Heads up. The fleet kill switch is engaged, so no agent can change "
                     "anything right now.")

    if pending:
        parts.append(f"{pending} action{'s are' if pending != 1 else ' is'} waiting for your "
                     f"approval. Nothing moves on {'those' if pending != 1 else 'that'} until you decide.")

    if sf:
        line = (f"{sf} silent failure{'s' if sf != 1 else ''} caught since the last reset: "
                f"an agent reported success that the records do not support.")
        if worst:
            line += f" The one to look at is {worst.get('subject', 'an open run')}."
        parts.append(line)
    else:
        parts.append("No silent failures caught since the last reset.")

    if rate is not None:
        parts.append(f"Across {runs} runs, {verified} verified clean, and the silent failure "
                     f"rate is {rate * 100:.1f} percent.")

    parts.append("That is the whole picture. Everything else is on the board.")
    return " ".join(parts)


def _wav(pcm: bytes, rate: int = _PCM_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _speak_sync(script: str) -> bytes:
    client = genai.Client(**config.client_kwargs_for(config.BRIEFING_MODEL))
    resp = client.models.generate_content(
        model=config.BRIEFING_MODEL,
        contents=f"Read this shift briefing in a calm, level operations-room voice. "
                 f"Do not add anything, do not greet, do not editorialise:\n\n{script}",
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=config.BRIEFING_VOICE))),
        ),
    )
    part = resp.candidates[0].content.parts[0]
    return _wav(part.inline_data.data)


async def speak(script: str) -> bytes:
    """Render the briefing to WAV bytes."""
    return await asyncio.to_thread(_speak_sync, script)
