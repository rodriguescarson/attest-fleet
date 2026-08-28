"""Publish the fleet's agents to Google's Agent Registry (Gemini Enterprise Agent Platform).

    uv run python scripts/register_agents.py            # publish/update all five
    uv run python scripts/register_agents.py --list     # show what is registered

Each agent becomes a registry `Service` carrying an A2A agent card, so the registry
indexes its skills and an enterprise can discover it the same way it discovers Google's
own agents. `GET /fleet/identities` then reads those entries back, which is what makes the
endpoint an actual registry view rather than a hardcoded manifest.

Needs `agentregistry.googleapis.com` enabled and ADC with `agentregistry.services.create`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from attest_fleet import config  # noqa: E402
from attest_fleet.agents import AGENT_IDENTITIES  # noqa: E402

BASE = "https://agentregistry.googleapis.com/v1"
FLEET_URL = "https://attest-fleet-434066362046.asia-south1.run.app"


def token() -> str:
    import google.auth
    import google.auth.transport.requests

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def agent_card(a: dict) -> dict:
    """An A2A agent card. Each tool the agent may call becomes an indexed skill, tagged
    with whether it mutates the system of record — so the registry itself records which
    agents can change state."""
    skills = [
        {
            "id": t,
            "name": t,
            "description": f"{t} ({'mutating' if a['mutates'] else 'read-only'})",
            "tags": ["attest-fleet", a["name"], "mutating" if a["mutates"] else "read-only"],
        }
        for t in a["tools"]
    ]
    if not skills:
        skills = [{"id": f"{a['name']}-verify", "name": "verify",
                   "description": a["role"], "tags": ["attest-fleet", a["name"]]}]
    return {
        "supportedInterfaces": [{"url": f"{FLEET_URL}/fleet/identities",
                                 "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0.0"}],
        "name": a["name"],
        "description": a["role"],
        "version": "1.0.0",
        "capabilities": {"streaming": False, "pushNotifications": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": skills,
    }


def call(method: str, url: str, body: dict | None, tok: str):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed Google endpoint
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list registered agents and exit")
    args = ap.parse_args()

    project = config.VERTEX_PROJECT
    if not project:
        sys.exit("set GOOGLE_CLOUD_PROJECT first")
    loc = config.REGISTRY_LOCATION
    tok = token()

    if args.list:
        data = call("GET", f"{BASE}/projects/{project}/locations/{loc}/agents", None, tok)
        for a in data.get("agents", []):
            print(f"  {a.get('displayName'):20s} skills={len(a.get('skills', []))} {a.get('agentId','')[:70]}")
        return

    services = f"{BASE}/projects/{project}/locations/{loc}/services"
    for a in AGENT_IDENTITIES:
        sid = "attest-" + a["name"].replace("_", "-")
        body = {
            "displayName": f"Attest Fleet · {a['name']}",
            "description": f"{a['role']} — model {a['model']}. Mutating: {a['mutates']}.",
            "agentSpec": {"type": "A2A_AGENT_CARD", "content": agent_card(a)},
        }
        try:
            call("POST", f"{services}?serviceId={sid}", body, tok)
            print(f"  registered {sid}")
        except urllib.error.HTTPError as e:
            if e.code == 409:  # already there: update it
                call("PATCH", f"{services}/{sid}?updateMask=displayName,description,agentSpec", body, tok)
                print(f"  updated    {sid}")
            else:
                print(f"  FAILED     {sid}: {e.code} {e.read()[:200]!r}")


if __name__ == "__main__":
    main()
