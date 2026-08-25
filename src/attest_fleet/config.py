"""Runtime configuration. Everything comes from the environment so the same image
runs locally (memory store, AI Studio key) and on Cloud Run (Firestore, same key
or Vertex)."""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

APP_NAME = "attest_fleet"

# Gemini 3.5 is the hackathon gate; 3.5 Flash-Lite is the cheap worker tier.
CONTROLLER_MODEL = os.getenv("ATTEST_CONTROLLER_MODEL", "gemini-3.5-flash-lite")
WORKER_MODEL = os.getenv("ATTEST_WORKER_MODEL", "gemini-3.5-flash-lite")
AUDITOR_MODEL = os.getenv("ATTEST_AUDITOR_MODEL", "gemma-4-31b-it")  # different family from the workers: independent verification (+ ATA extra-model bonus)
VISION_MODEL = os.getenv("ATTEST_VISION_MODEL", "gemini-3.5-flash-lite")  # multimodal intake: reads screenshots attached to a ticket

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

# Lessons injected into worker instructions from past verified failures.
PLAYBOOK_LESSONS = int(os.getenv("ATTEST_PLAYBOOK_LESSONS", "5"))

# Mount the ADK developer UI under /adk (local demo only; off on Cloud Run by default).
ADK_UI = os.getenv("ATTEST_ADK_UI", "0") == "1"
