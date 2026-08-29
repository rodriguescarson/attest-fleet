"""Runtime configuration. Everything comes from the environment so the same image
runs locally (memory store, AI Studio key) and on Cloud Run (Firestore, same key
or Vertex)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "attest_fleet"

# Gemini 3.7 Flash (newest agent-tuned Flash, Aug 2026) is the fleet tier; 3.5 is the hackathon floor.
CONTROLLER_MODEL = os.getenv("ATTEST_CONTROLLER_MODEL", "gemini-3.7-flash")
WORKER_MODEL = os.getenv("ATTEST_WORKER_MODEL", "gemini-3.7-flash")
AUDITOR_MODEL = os.getenv("ATTEST_AUDITOR_MODEL", "gemma-4-31b-it")  # different family from the workers: independent verification (+ ATA extra-model bonus)
VISION_MODEL = os.getenv("ATTEST_VISION_MODEL", "gemini-3.7-flash")
VOICE_MODEL = os.getenv("ATTEST_VOICE_MODEL", "gemini-3.7-flash")  # audio intake: Gemini takes audio natively
# TTS is Developer-API only (Vertex rejects the AUDIO response modality), same split as Gemma.
BRIEFING_MODEL = os.getenv("ATTEST_BRIEFING_MODEL", "gemini-2.5-flash-preview-tts")
BRIEFING_VOICE = os.getenv("ATTEST_BRIEFING_VOICE", "Kore")  # multimodal intake: reads screenshots attached to a ticket

# --- Model fallback cascade ---------------------------------------------------
# A brand-new Gemini Flash is demand-shed (HTTP 503) in the days after launch. Rather
# than fail a ticket, the fleet degrades to the next tier on a transient error. Gemini
# agents fall back within Gemini; the auditor falls back within the Gemma family so it
# stays an INDEPENDENT verifier (a different model family from the workers).
def _chain(*models: str) -> list:
    seen, out = set(), []
    for m in models:
        if m and m not in seen:
            seen.add(m); out.append(m)
    return out

# --- Backend routing ----------------------------------------------------------
# Gemini roles run on VERTEX AI, billed to the project, so the fleet is not capped by the
# Gemini Developer API free tier. Gemma is not served as a Vertex publisher model, so the
# auditor keeps the Developer API key. That is not a workaround: it means the auditor
# reaches its model through a different BACKEND as well as a different model FAMILY, which
# is exactly the independence an auditor is supposed to have.
USE_VERTEX = os.getenv("ATTEST_USE_VERTEX", "1") == "1"
VERTEX_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT", "")
VERTEX_LOCATION = os.getenv("ATTEST_VERTEX_LOCATION", "global")  # 3.7/3.6 are global-only today
REGISTRY_LOCATION = os.getenv("ATTEST_REGISTRY_LOCATION", "global")  # Agent Registry catalog

# Model Armor: inline guardrail screening untrusted ticket text for prompt injection.
MODEL_ARMOR_TEMPLATE = os.getenv("ATTEST_MODEL_ARMOR_TEMPLATE", "attest-ticket-guard")
MODEL_ARMOR_LOCATION = os.getenv("ATTEST_MODEL_ARMOR_LOCATION", "asia-south1")


# Two model families are Developer-API only: Gemma is not a Vertex publisher model, and
# Vertex rejects the AUDIO response modality that text-to-speech needs. Everything else
# runs on Vertex, project-billed.
_DEV_API_ONLY = ("gemma", "-tts")


def on_vertex(model: str) -> bool:
    """Should this model be served by Vertex rather than the Gemini Developer API?"""
    name = (model or "").lower()
    if any(marker in name for marker in _DEV_API_ONLY):
        return False
    return USE_VERTEX and bool(VERTEX_PROJECT)


def client_kwargs_for(model: str) -> dict:
    if on_vertex(model):
        return {"vertexai": True, "project": VERTEX_PROJECT, "location": VERTEX_LOCATION}
    return {"api_key": os.getenv("GOOGLE_API_KEY", "")}


# The last rung differs by backend: 3.5-flash-lite exists on the Developer API,
# 3.5-flash is the equivalent floor on Vertex.
_FLOOR = "gemini-3.5-flash" if (USE_VERTEX and VERTEX_PROJECT) else "gemini-3.5-flash-lite"

CONTROLLER_CHAIN = _chain(CONTROLLER_MODEL, "gemini-3.6-flash", _FLOOR)
WORKER_CHAIN = _chain(WORKER_MODEL, "gemini-3.6-flash", _FLOOR)
VISION_CHAIN = _chain(VISION_MODEL, "gemini-3.6-flash", _FLOOR)
AUDITOR_CHAIN = _chain(AUDITOR_MODEL, "gemma-4-26b-a4b-it")

MODEL_CHAINS = {
    "fleet_controller": CONTROLLER_CHAIN,
    "billing_agent": WORKER_CHAIN,
    "account_agent": WORKER_CHAIN,
    "vision_reader": VISION_CHAIN,
    "auditor": AUDITOR_CHAIN,
}

# "memory" for local dev/tests, "firestore" on Google Cloud.
STORE_BACKEND = os.getenv("ATTEST_STORE", "memory")
FIRESTORE_DATABASE = os.getenv("ATTEST_FIRESTORE_DATABASE", "(default)")
GOOGLE_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")

# Policy: refunds above this amount need a human approval before the tool runs.
REFUND_APPROVAL_THRESHOLD = float(os.getenv("ATTEST_REFUND_APPROVAL_THRESHOLD", "100"))

# Fault injection for the eval harness. 0 in production.
FAULT_RATE = float(os.getenv("ATTEST_FAULT_RATE", "0"))

# The verifier recommends the lowest confidence threshold whose residual
# silent-failure rate is at or below this target.
TARGET_RESIDUAL_RISK = float(os.getenv("ATTEST_TARGET_RESIDUAL_RISK", "0.02"))
STREAM_INTERVAL_S = float(os.getenv("ATTEST_STREAM_INTERVAL_S", "2"))  # SSE digest cadence

# Loop containment. A worker that never raises is otherwise bounded only by the Cloud Run
# request timeout, so a runaway agent is capped two ways: a wall-clock budget per model
# turn, and a hard ceiling on tool calls per task enforced at the same gate that enforces
# policy. Both fail closed and leave an evidence event.
AGENT_TURN_TIMEOUT_S = float(os.getenv("ATTEST_AGENT_TURN_TIMEOUT_S", "90"))
MAX_TOOL_CALLS_PER_TASK = int(os.getenv("ATTEST_MAX_TOOL_CALLS_PER_TASK", "12"))

# Lessons injected into worker instructions from past verified failures.
PLAYBOOK_LESSONS = int(os.getenv("ATTEST_PLAYBOOK_LESSONS", "5"))

# Mount the ADK developer UI under /adk (local demo only; off on Cloud Run by default).
ADK_UI = os.getenv("ATTEST_ADK_UI", "0") == "1"
