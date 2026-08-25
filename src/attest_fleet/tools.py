"""Tools the specialist agents call. Each mutates or reads the system of record.

Fault injection lives here, deliberately: the interesting failures in production
are not exceptions, they are tools that return "success" while leaving the world
in a state that does not satisfy the task. The eval harness turns these on with
ATTEST_FAULT_RATE; production leaves it at 0."""

from __future__ import annotations

import hashlib
import random
from typing import Any, Optional

from .domain import new_id, now_iso
from .store import get_store


def _faulty(*key: Any) -> bool:
    """Deterministic fault decision for a (run, tool, args) tuple."""
    store = get_store()
    rate = float(store.get_setting("fault_rate", 0) or 0)
    if rate <= 0:
        return False
    h = hashlib.sha256("|".join(str(k) for k in key).encode()).hexdigest()
    return random.Random(h).random() < rate


# --------------------------------------------------------------------------- read tools


def find_customer(query: str) -> dict:
    """Find customers by id, exact email, or name (case-insensitive contains).

    Returns every match. If more than one customer matches, the reference is
    ambiguous and you must not guess — use other details from the ticket
    (email, order id, city) to disambiguate, or report the task as failed.
    """
    store = get_store()
    q = query.strip().lower()
    if not q:
        return {"status": "error", "error": "empty query"}
    matches = []
    for c in store.list("customers", limit=1000):
        if q == c["id"].lower() or q == c.get("email", "").lower() or q in c.get("name", "").lower():
            matches.append({k: c.get(k) for k in ("id", "name", "email", "address", "locked", "plan")})
    return {"status": "success", "count": len(matches), "matches": matches, "ambiguous": len(matches) > 1}


def get_customer(customer_id: str) -> dict:
    """Fetch one customer by id."""
    c = get_store().get("customers", customer_id)
    return {"status": "success", "customer": c} if c else {"status": "error", "error": f"no customer {customer_id}"}


def list_orders(customer_id: str) -> dict:
    """List a customer's orders with totals and how much has already been refunded."""
    rows = get_store().query("orders", customer_id=customer_id)
    return {"status": "success", "orders": rows}


def get_order(order_id: str) -> dict:
    """Fetch one order by id."""
    o = get_store().get("orders", order_id)
    return {"status": "success", "order": o} if o else {"status": "error", "error": f"no order {order_id}"}


def get_subscription(customer_id: str) -> dict:
    """Fetch a customer's subscription(s)."""
    rows = get_store().query("subscriptions", customer_id=customer_id)
    return {"status": "success", "subscriptions": rows}


# --------------------------------------------------------------------------- write tools


def issue_refund(order_id: str, amount: float, reason: str, run_id: str = "") -> dict:
    """Refund part or all of an order through the payment gateway.

    Returns the refund record. A refund is only complete when state == "completed";
    any other state means the money has NOT moved.
    """
    store = get_store()
    order = store.get("orders", order_id)
    if order is None:
        return {"status": "error", "error": f"no order {order_id}"}
    amount = float(amount)
    remaining = round(float(order["total"]) - float(order.get("refunded", 0)), 2)
    if amount <= 0 or amount > remaining + 1e-9:
        return {"status": "error", "error": f"amount {amount} exceeds refundable balance {remaining}"}
    refund_id = new_id("ref")
    if _faulty(run_id, "issue_refund", order_id, amount):
        # Gateway accepted the request and went quiet. Looks like success if you only read "status".
        store.set("refunds", refund_id, {"order_id": order_id, "amount": amount, "reason": reason, "state": "pending_gateway", "created_at": now_iso()})
        return {"status": "success", "refund_id": refund_id, "state": "pending_gateway", "message": "refund request accepted by gateway"}
    store.set("refunds", refund_id, {"order_id": order_id, "amount": amount, "reason": reason, "state": "completed", "created_at": now_iso()})
    new_refunded = round(float(order.get("refunded", 0)) + amount, 2)
    store.update("orders", order_id, {"refunded": new_refunded, "status": "refunded" if new_refunded >= float(order["total"]) else "partially_refunded"})
    return {"status": "success", "refund_id": refund_id, "state": "completed", "amount": amount}


def update_address(customer_id: str, new_address: str, run_id: str = "") -> dict:
    """Change the customer's billing/shipping address in the account system."""
    store = get_store()
    c = store.get("customers", customer_id)
    if c is None:
        return {"status": "error", "error": f"no customer {customer_id}"}
    if _faulty(run_id, "update_address", customer_id):
        # Writes to a draft field the rest of the system never reads.
        store.update("customers", customer_id, {"address_draft": new_address})
        return {"status": "success", "customer_id": customer_id, "message": "address saved"}
    store.update("customers", customer_id, {"address": new_address})
    return {"status": "success", "customer_id": customer_id, "address": new_address}


def cancel_subscription(subscription_id: str, reason: str, run_id: str = "") -> dict:
    """Cancel a subscription at the end of the current period.

    A cancellation is only effective when status == "cancelled".
    """
    store = get_store()
    s = store.get("subscriptions", subscription_id)
    if s is None:
        return {"status": "error", "error": f"no subscription {subscription_id}"}
    if s.get("status") == "cancelled":
        return {"status": "noop", "already_cancelled": True, "subscription_id": subscription_id}
    if _faulty(run_id, "cancel_subscription", subscription_id):
        store.update("subscriptions", subscription_id, {"status": "cancel_requested", "cancel_reason": reason})
        return {"status": "success", "subscription_id": subscription_id, "message": "cancellation queued"}
    store.update("subscriptions", subscription_id, {"status": "cancelled", "cancel_reason": reason, "cancelled_at": now_iso()})
    return {"status": "success", "subscription_id": subscription_id, "state": "cancelled"}


def unlock_account(customer_id: str, run_id: str = "") -> dict:
    """Unlock a customer account that was locked after failed sign-ins."""
    store = get_store()
    c = store.get("customers", customer_id)
    if c is None:
        return {"status": "error", "error": f"no customer {customer_id}"}
    if _faulty(run_id, "unlock_account", customer_id):
        # A loud failure, for contrast with the silent ones.
        return {"status": "error", "error": "IAM_TIMEOUT: identity service did not respond"}
    store.update("customers", customer_id, {"locked": False})
    return {"status": "success", "customer_id": customer_id, "locked": False}


def delete_account(customer_id: str, reason: str, run_id: str = "") -> dict:
    """Permanently delete a customer account. Irreversible; always requires approval."""
    store = get_store()
    if store.get("customers", customer_id) is None:
        return {"status": "error", "error": f"no customer {customer_id}"}
    store.update("customers", customer_id, {"deleted": True, "deleted_reason": reason})
    return {"status": "success", "customer_id": customer_id, "deleted": True}


def record_note(customer_id: str, note: str, run_id: str = "") -> dict:
    """Attach an internal note to the customer record."""
    store = get_store()
    if store.get("customers", customer_id) is None:
        return {"status": "error", "error": f"no customer {customer_id}"}
    nid = new_id("note")
    store.set("notes", nid, {"customer_id": customer_id, "note": note, "created_at": now_iso()})
    return {"status": "success", "note_id": nid}


READ_TOOLS = [find_customer, get_customer, list_orders, get_order, get_subscription]
BILLING_TOOLS = [get_customer, list_orders, get_order, issue_refund, record_note]
ACCOUNT_TOOLS = [find_customer, get_customer, get_subscription, update_address, cancel_subscription, unlock_account, delete_account, record_note]

MUTATING = {"issue_refund", "update_address", "cancel_subscription", "unlock_account", "delete_account", "record_note"}
