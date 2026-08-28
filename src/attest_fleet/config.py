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
VISION_MODEL = os.getenv("ATTEST_VISION_MODEL", "gemini-3.7-flash")  # multimodal intake: reads screenshots attached to a ticket

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

CONTROLLER_CHAIN = _chain(CONTROLLER_MODEL, "gemini-3.6-flash", "gemini-3.5-flash-lite")
WORKER_CHAIN = _chain(WORKER_MODEL, "gemini-3.6-flash", "gemini-3.5-flash-lite")
VISION_CHAIN = _chain(VISION_MODEL, "gemini-3.6-flash", "gemini-3.5-flash-lite")
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
