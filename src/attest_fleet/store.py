"""The system of record and the evidence store, behind one small interface.

MemoryStore runs tests and local demos with no cloud at all. FirestoreStore is
the same API on Google Cloud Firestore. Both hold the business data the agents
act on (customers, orders, subscriptions, refunds) and the evidence Attest
captures (runs, events, approvals, playbook)."""

from __future__ import annotations

import copy
import threading
from typing import Any, Iterable, Optional

from . import config

Doc = dict[str, Any]

BUSINESS_COLLECTIONS = ("customers", "orders", "subscriptions", "refunds", "notes")
EVIDENCE_COLLECTIONS = ("runs", "events", "approvals", "playbook", "settings")


class BaseStore:
    backend = "base"

    def get(self, col: str, doc_id: str) -> Optional[Doc]:
        raise NotImplementedError

    def set(self, col: str, doc_id: str, doc: Doc) -> None:
        raise NotImplementedError

    def update(self, col: str, doc_id: str, patch: Doc) -> None:
        raise NotImplementedError

    def delete(self, col: str, doc_id: str) -> None:
        raise NotImplementedError

    def query(self, col: str, **eq: Any) -> list[Doc]:
        raise NotImplementedError

    def list(self, col: str, limit: int = 100) -> list[Doc]:
        raise NotImplementedError

    # settings are one doc per key: {"value": ...}
    def get_setting(self, key: str, default: Any = None) -> Any:
        d = self.get("settings", key)
        return default if d is None else d.get("value", default)

    def set_setting(self, key: str, value: Any) -> None:
        self.set("settings", key, {"value": value})

    def count(self, col: str) -> int:
        return len(self.list(col, limit=10_000))


class MemoryStore(BaseStore):
    backend = "memory"

    def __init__(self) -> None:
        self._d: dict[str, dict[str, Doc]] = {}
        self._lock = threading.RLock()

    def _col(self, col: str) -> dict[str, Doc]:
        return self._d.setdefault(col, {})

    def get(self, col, doc_id):
        with self._lock:
            d = self._col(col).get(doc_id)
            return copy.deepcopy(d) if d is not None else None

    def set(self, col, doc_id, doc):
        with self._lock:
            self._col(col)[doc_id] = copy.deepcopy({**doc, "id": doc_id})

    def update(self, col, doc_id, patch):
        with self._lock:
            cur = self._col(col).get(doc_id)
            if cur is None:
                raise KeyError(f"{col}/{doc_id} does not exist")
            cur.update(copy.deepcopy(patch))

    def delete(self, col, doc_id):
        with self._lock:
            self._col(col).pop(doc_id, None)

    def query(self, col, **eq):
        with self._lock:
            out = [copy.deepcopy(d) for d in self._col(col).values() if all(d.get(k) == v for k, v in eq.items())]
        return out

    def list(self, col, limit=100):
        with self._lock:
            return [copy.deepcopy(d) for d in list(self._col(col).values())[-limit:]]


class FirestoreStore(BaseStore):
    backend = "firestore"

    def __init__(self, project: Optional[str] = None, database: str = "(default)") -> None:
        from google.cloud import firestore  # imported lazily so tests never need it

        self._fs = firestore.Client(project=project, database=database)
        self._firestore = firestore

    def get(self, col, doc_id):
        snap = self._fs.collection(col).document(doc_id).get()
        return {**snap.to_dict(), "id": snap.id} if snap.exists else None

    def set(self, col, doc_id, doc):
        self._fs.collection(col).document(doc_id).set({**doc, "id": doc_id})

    def update(self, col, doc_id, patch):
        self._fs.collection(col).document(doc_id).update(patch)

    def delete(self, col, doc_id):
        self._fs.collection(col).document(doc_id).delete()

    def query(self, col, **eq):
        q = self._fs.collection(col)
        for k, v in eq.items():
            q = q.where(filter=self._firestore.FieldFilter(k, "==", v))
        return [{**s.to_dict(), "id": s.id} for s in q.stream()]

    def list(self, col, limit=100):
        q = self._fs.collection(col).limit(limit)
        return [{**s.to_dict(), "id": s.id} for s in q.stream()]


_store: Optional[BaseStore] = None
_store_lock = threading.Lock()


def get_store() -> BaseStore:
    global _store
    with _store_lock:
        if _store is None:
            if config.STORE_BACKEND == "firestore":
                _store = FirestoreStore(project=config.GOOGLE_CLOUD_PROJECT, database=config.FIRESTORE_DATABASE)
            else:
                _store = MemoryStore()
        return _store


def use_store(store: BaseStore) -> None:
    """Tests and the simulator swap in a fresh store."""
    global _store
    with _store_lock:
        _store = store


# ---------------------------------------------------------------------------
# Seed data: a small SaaS. Two customers share a name on purpose — that is the
# most common way a support agent acts on the wrong account.
# ---------------------------------------------------------------------------

SEED_CUSTOMERS: list[Doc] = [
    {"id": "cus_1001", "name": "Priya Sharma", "email": "priya.sharma@example.com", "address": "14 Lake View Rd, Pune 411001", "locked": False, "plan": "pro"},
    {"id": "cus_1002", "name": "Priya Sharma", "email": "priya.s@example.org", "address": "77 MG Road, Bengaluru 560001", "locked": True, "plan": "starter"},
    {"id": "cus_1003", "name": "Daniel Okafor", "email": "d.okafor@example.com", "address": "5 Marina Way, Lagos", "locked": False, "plan": "pro"},
    {"id": "cus_1004", "name": "Mei-Ling Chen", "email": "meiling@example.com", "address": "88 Nanjing Rd, Shanghai 200001", "locked": False, "plan": "team"},
    {"id": "cus_1005", "name": "Carlos Duarte", "email": "carlos@example.pt", "address": "Rua Augusta 12, Lisboa 1100-048", "locked": True, "plan": "starter"},
    {"id": "cus_1006", "name": "Aisha Rahman", "email": "aisha.r@example.com", "address": "Flat 3B, Park St, Kolkata 700016", "locked": False, "plan": "pro"},
]

SEED_ORDERS: list[Doc] = [
    {"id": "ord_5001", "customer_id": "cus_1001", "total": 49.0, "refunded": 0.0, "item": "Pro monthly", "status": "paid"},
    {"id": "ord_5002", "customer_id": "cus_1002", "total": 19.0, "refunded": 0.0, "item": "Starter monthly", "status": "paid"},
    {"id": "ord_5003", "customer_id": "cus_1003", "total": 490.0, "refunded": 0.0, "item": "Pro annual", "status": "paid"},
    {"id": "ord_5004", "customer_id": "cus_1004", "total": 1490.0, "refunded": 0.0, "item": "Team annual", "status": "paid"},
    {"id": "ord_5005", "customer_id": "cus_1005", "total": 19.0, "refunded": 19.0, "item": "Starter monthly", "status": "refunded"},
    {"id": "ord_5006", "customer_id": "cus_1006", "total": 49.0, "refunded": 0.0, "item": "Pro monthly", "status": "paid"},
    {"id": "ord_5007", "customer_id": "cus_1001", "total": 120.0, "refunded": 0.0, "item": "Add-on seats", "status": "paid"},
]

SEED_SUBSCRIPTIONS: list[Doc] = [
    {"id": "sub_9001", "customer_id": "cus_1001", "plan": "pro", "status": "active"},
    {"id": "sub_9002", "customer_id": "cus_1002", "plan": "starter", "status": "active"},
    {"id": "sub_9003", "customer_id": "cus_1003", "plan": "pro", "status": "active"},
    {"id": "sub_9004", "customer_id": "cus_1004", "plan": "team", "status": "active"},
    {"id": "sub_9005", "customer_id": "cus_1005", "plan": "starter", "status": "cancelled"},
    {"id": "sub_9006", "customer_id": "cus_1006", "plan": "pro", "status": "active"},
]


def seed(store: BaseStore, force: bool = False) -> bool:
    """Load the demo business data. Idempotent unless force=True."""
    if not force and store.get("customers", "cus_1001") is not None:
        return False
    for col, rows in (("customers", SEED_CUSTOMERS), ("orders", SEED_ORDERS), ("subscriptions", SEED_SUBSCRIPTIONS)):
        for row in rows:
            store.set(col, row["id"], {k: v for k, v in row.items() if k != "id"})
    for col in ("refunds", "notes"):
        for d in store.list(col, limit=10_000):
            store.delete(col, d["id"])
    store.set_setting("kill_switch", False)
    store.set_setting("fault_rate", config.FAULT_RATE)
    return True


def reset_evidence(store: BaseStore) -> None:
    for col in ("runs", "events", "approvals", "playbook"):
        for d in store.list(col, limit=10_000):
            store.delete(col, d["id"])
