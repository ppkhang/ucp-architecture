"""cancel_order (UC-03). The platform decides (ADR-006). The user agrees on the confirmation page first (ADR-007),
except for an order that is not paid: no money has moved, so the agent may cancel it and the user is alerted."""
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, Field

from ucp.adapters import get_adapter
from ucp.adapters.base import PlatformTimeout
from ucp.common.config import get_settings
from ucp.common.db import connect
from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.identity import service as identity
from ucp.modules.trade import service

REQUEST_TTL = 1800  # seconds the user has to agree, and the agent has to finish
UNPAID = ("pending", "failed")  # payment statuses where no money has moved or is moving


class CancelOrderParams(BaseModel):
    order_id: uuid.UUID
    reason: str | None = Field(None, max_length=500)  # accepted, never shown to the user or sent on


def _result(order_id: str, refund_ref: str | None, shop: dict) -> dict:
    # order_url is the platform's own page for the order, for the agent to show the user
    return {"order_id": order_id, "status": "cancelled", "refund_ref": refund_ref, "order_url": shop.get("order_url")}


def cancel_order(agent_id: str, idem_key: str, raw: dict, http: httpx.Client) -> dict:
    p = service.parse(CancelOrderParams, raw)
    order_id = str(p.order_id)
    with connect() as conn:
        order = conn.execute(
            "SELECT o.*, c.platform_user, c.proposal FROM trade.orders o JOIN trade.checkouts c ON c.id = o.checkout_id "
            "WHERE o.id = %s AND o.agent_id = %s", (order_id, agent_id)).fetchone()
    if not order:
        raise UCPError(ErrorCode.NOT_FOUND, "order not found")
    # cancel_order needs a live UserLink, checked on every call (hard rule 11)
    link = identity.get_link(agent_id, link_id=str(order["link_id"]), http=http)
    if order["status"] == "cancelled":
        try:
            shop = get_adapter(order["platform_id"], http).get_order(link["access_token"], order["platform_order_id"])
        except UCPError:
            shop = {}  # the order is cancelled either way, only the link is missing
        return _result(order_id, order["refund_ref"], shop)
    if order["reconciling"] or not order["platform_order_id"]:
        raise UCPError(ErrorCode.BAD_REQUEST, "the order is still being confirmed with the shop, try again later")
    # No refusal from the cached status: it can be stale, so only the platform says no (ADR-006).
    adapter = get_adapter(order["platform_id"], http)
    token, shop_id = link["access_token"], order["platform_order_id"]
    shop = adapter.get_order(token, shop_id)  # live: also finds a cancel whose answer was lost
    if shop["status"] == "cancelled":
        _apply_cancel(order_id, shop, shop.get("refund_ref"))
        return _result(order_id, shop.get("refund_ref"), shop)
    if shop["status"] == "pending_payment" and shop["payment_status"] in UNPAID:
        unconfirmed = _cancel_unpaid(adapter, token, shop_id, f"{agent_id}:{idem_key}", order_id)
        if unconfirmed:
            return unconfirmed  # None: it was paid meanwhile, so the user has to agree after all
    with connect() as conn:
        # Use up the confirmation. One that was used but got no answer is resumed with the same key.
        req = conn.execute(
            "UPDATE trade.cancel_requests SET idempotency_key = coalesce(idempotency_key, %s) WHERE id = ("
            "SELECT id FROM trade.cancel_requests WHERE order_id = %s AND agent_id = %s AND outcome IS NULL "
            "AND confirmed_at IS NOT NULL AND confirmed_hash = snapshot_hash AND confirmed_user = platform_user "
            "AND expires_at > now() ORDER BY created_at LIMIT 1 FOR UPDATE) "
            "RETURNING id, idempotency_key", (f"{agent_id}:{idem_key}", order_id, agent_id)).fetchone()
    if not req:
        _raise_if_refused(order_id, agent_id)
        raise _needs_confirmation(order, agent_id)
    try:
        answer = adapter.cancel_order(token, shop_id, req["idempotency_key"])
        shop, refund_ref = answer["order"], answer["refund_ref"]
    except PlatformTimeout:
        shop = adapter.get_order(token, shop_id)
        if shop["status"] != "cancelled":
            raise  # unknown: the confirmation stays usable for a retry, which resends the same key
        refund_ref = shop.get("refund_ref")
    except UCPError as e:
        if e.code == ErrorCode.CANNOT_CANCEL:
            _settle(req["id"], "refused")
        raise
    _apply_cancel(order_id, shop, refund_ref)
    _settle(req["id"], "cancelled")
    return _result(order_id, refund_ref, shop)


def _raise_if_refused(order_id: str, agent_id: str) -> None:
    """The user agreed and the shop said no: answer that, instead of asking the user to agree again."""
    with connect() as conn:
        refused = conn.execute("SELECT 1 FROM trade.cancel_requests WHERE order_id = %s AND agent_id = %s "
                               "AND outcome = 'refused' AND expires_at > now() LIMIT 1", (order_id, agent_id)).fetchone()
    if refused:
        raise UCPError(ErrorCode.CANNOT_CANCEL, "the shop refused to cancel this order")


def _cancel_unpaid(adapter: Any, token: str, shop_id: str, key: str, order_id: str) -> dict | None:
    """Cancel without asking. The platform only does it while the order is still unpaid."""
    try:
        answer = adapter.cancel_order(token, shop_id, key, only_if_unpaid=True)
        shop, refund_ref = answer["order"], answer["refund_ref"]
    except PlatformTimeout:
        shop = adapter.get_order(token, shop_id)
        if shop["status"] != "cancelled":
            raise  # unknown: a retry with the same key is safe
        refund_ref = shop.get("refund_ref")
    except UCPError as e:
        if e.code == ErrorCode.CONFIRMATION_REQUIRED:
            return None
        raise
    _apply_cancel(order_id, shop, refund_ref)
    return _result(order_id, refund_ref, shop) | {"confirmed_by_user": False}


def _apply_cancel(order_id: str, shop: dict, refund_ref: str | None) -> None:
    with connect() as conn:
        row = conn.execute("SELECT id, agent_id, platform_id, status, payment_status FROM trade.orders "
                           "WHERE id = %s FOR UPDATE", (order_id,)).fetchone()
        service.apply_status(conn, row, "cancelled", shop["payment_status"])
        service.save_refund_ref(conn, order_id, refund_ref)


def _settle(request_id: str, outcome: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE trade.cancel_requests SET outcome = %s WHERE id = %s", (outcome, request_id))


def _needs_confirmation(order: dict, agent_id: str) -> UCPError:
    """Open a cancel request (or reuse the one still waiting) and tell the agent where to send the user."""
    snapshot = {"order_id": str(order["id"]), "platform_order_id": order["platform_order_id"],
                "items": [{"name": i["name"], "qty": i["qty"]} for i in order["proposal"]["items"]],
                "total": order["total"]}
    digest = service.snapshot_hash(snapshot)
    with connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 2))", (str(order["id"]),))
        req = conn.execute(
            "SELECT id, expires_at FROM trade.cancel_requests WHERE order_id = %s AND agent_id = %s "
            "AND idempotency_key IS NULL AND snapshot_hash = %s AND expires_at > now() + interval '1 minute' "
            "ORDER BY created_at DESC LIMIT 1", (order["id"], agent_id, digest)).fetchone()
        if not req:
            req = {"id": uuid.uuid4(), "expires_at": datetime.now(UTC) + timedelta(seconds=REQUEST_TTL)}
            conn.execute(
                "INSERT INTO trade.cancel_requests (id, order_id, agent_id, platform_user, snapshot, snapshot_hash, "
                "expires_at) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
                (req["id"], order["id"], agent_id, order["platform_user"], json.dumps(snapshot), digest,
                 req["expires_at"]))
    return UCPError(ErrorCode.CONFIRMATION_REQUIRED, "the user has to agree to this cancel",
                    {"confirm_url": f"{get_settings().public_url}/v1/confirm/{req['id']}",
                     "expires_at": req["expires_at"].astimezone(UTC).isoformat().replace("+00:00", "Z")})
