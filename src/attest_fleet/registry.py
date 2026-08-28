"""Agent Registry integration (Gemini Enterprise Agent Platform).

The fleet's agents are published to Google's Agent Registry as A2A agent cards, one
`Service` each, and this module reads them back. That is the point: `/fleet/identities`
is not a hardcoded manifest that merely claims to be a registry — it is a live read from
the platform's own discovery plane, so what the dashboard shows is what an enterprise
would actually discover when looking for approved agents.

Registration is done once by `scripts/register_agents.py`. Reads are cached and fall back
to the in-code identity list, so local development and the tests never need cloud access.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Optional

from . import config
from .agents import AGENT_IDENTITIES

_BASE = "https://agentregistry.googleapis.com/v1"
_cache: dict[str, Any] = {"at": 0.0, "agents": None, "source": "local"}
_TTL_S = 300.0


def _token() -> Optional[str]:
    """Metadata-server token on Cloud Run; ADC locally. Absent means no registry read."""
    try:
        import google.auth
        import google.auth.transport.requests

        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token
    except Exception:  # noqa: BLE001 - no credentials is a normal local state
        return None


def _fetch() -> Optional[list[dict]]:
    project, location = config.VERTEX_PROJECT, config.REGISTRY_LOCATION
    if not project:
        return None
    token = _token()
    if not token:
        return None
    url = f"{_BASE}/projects/{project}/locations/{location}/agents"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - fixed Google endpoint
        return json.load(r).get("agents", [])


def registered_agents() -> tuple[list[dict], str]:
    """Fleet identities enriched with their Agent Registry entry.

    Returns (identities, source) where source is "agent-registry" when the live registry
    answered and "local" when we fell back. The local list still supplies the model and
    the mutates flag, which are deployment facts the registry does not carry.
    """
    now = time.time()
    if _cache["agents"] is not None and now - _cache["at"] < _TTL_S:
        return _cache["agents"], _cache["source"]

    by_name: dict[str, dict] = {}
    source = "local"
    try:
        for a in _fetch() or []:
            name = a.get("displayName")
            if name:
                by_name[name] = a
        if by_name:
            source = "agent-registry"
    except Exception:  # noqa: BLE001 - the registry is an enrichment, never a hard dependency
        by_name, source = {}, "local"

    out = []
    for ident in AGENT_IDENTITIES:
        entry = dict(ident)
        hit = by_name.get(ident["name"])
        if hit:
            entry["registry"] = {
                "resource": hit.get("name", "").split("/")[-1],
                "agent_id": hit.get("agentId"),
                "version": hit.get("version"),
                "skills": [s.get("id") for s in hit.get("skills", [])],
                "protocols": [p.get("type") for p in hit.get("protocols", [])],
            }
        entry["registered"] = bool(hit)
        out.append(entry)

    _cache.update({"at": now, "agents": out, "source": source})
    return out, source
