"""ADK discovery entry point for `adk web agents` — the interactive demo fleet."""

from attest_fleet.agents import build_chat_fleet
from attest_fleet.store import get_store, seed

seed(get_store())
root_agent = build_chat_fleet()
