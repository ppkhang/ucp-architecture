"""Trade context: checkouts, proposal snapshots, the user's confirmation, orders, platform webhooks.

The platform stays the source of truth for price, stock and orders (ADR-006). UCP stores what it quoted,
what the user agreed to, and a cache of the platform's order status."""
import hashlib
import hmac
import json
import secrets
import time
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from html import escape
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from pydantic import BaseModel, Field, ValidationError

from ucp import outbox
from ucp.adapters import get_adapter
from ucp.adapters.base import PlatformTimeout
from ucp.common.config import get_settings
from ucp.common.db import connect
from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.identity import service as identity

# UCP's cache of the platform's order status. Anything not in this table is rejected (AGENTS.md section 7).
ORDER_TRANSITIONS: dict[str, set[str]] = {
    "pending_payment": {"placed", "cancelled"},
    "pre_ordered": {"processing", "cancelled"},
    "placed": {"processing", "cancelled"},
    "processing": {"shipped", "cancelled"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}
# The platform said no and no order exists, so the row is dropped instead of left "reconciling".
REFUSED = {ErrorCode.INSUFFICIENT_STOCK, ErrorCode.CHECKOUT_EXPIRED, ErrorCode.NOT_FOUND, ErrorCode.BAD_REQUEST}
EVENT_OF = {"placed": "OrderPlaced", "cancelled": "OrderCancelled"}  # other moves are OrderStatusChanged
CONFIRM_LOGIN_TTL = 600
WEBHOOK_TOLERANCE = 300


class ItemIn(BaseModel):
    product_id: str
    variant_id: str | None = None
    qty: int = Field(ge=1, le=100)


class CreateCheckoutParams(BaseModel):
    platform_id: str
    items: list[ItemIn] = Field(min_length=1)
    region: str = "HCM"


class ConfirmOrderParams(BaseModel):
    checkout_id: uuid.UUID
    payment_method: str | None = None


def parse(model: type[BaseModel], params: dict) -> Any:
    try:
        return model.model_validate(params)
    except ValidationError as e:
        raise UCPError(ErrorCode.BAD_REQUEST, "invalid params",
                       {"errors": [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]}) from None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def _hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# -- create_checkout ---------------------------------------------------------

def create_checkout(agent_id: str, raw: dict, http: httpx.Client) -> dict:
    p = parse(CreateCheckoutParams, raw)
    adapter = get_adapter(p.platform_id, http)
    link = identity.get_link(agent_id, platform_id=p.platform_id, http=http)  # create_order needs a UserLink
    items, meta = [], {}
    for i in p.items:
        product = adapter.get_product(i.product_id)
        variants = product["variants"]
        if i.variant_id is None and len(variants) != 1:
            raise UCPError(ErrorCode.BAD_REQUEST, f"variant_id is required for {i.product_id}")
        v = next((v for v in variants if i.variant_id in (None, v["variant_id"])), None)
        if v is None:
            raise UCPError(ErrorCode.NOT_FOUND, f"variant {i.variant_id} not found")
        items.append({"product_id": i.product_id, "variant_id": v["variant_id"], "qty": i.qty})
        meta[v["variant_id"]] = (i.product_id, f"{product['name']} ({v['name']})")
    co = adapter.create_checkout(link["access_token"], items, p.region)
    hold = co["hold"]
    proposal = {
        "items": [{"product_id": meta[ln["variant_id"]][0], "variant_id": ln["variant_id"],
                   "name": meta[ln["variant_id"]][1], "qty": ln["quantity"], "unit_price": ln["unit_price"]}
                  for ln in co["lines"]],
        "subtotal": co["subtotal"], "shipping_fee": co["shipping_fee"], "discount": 0, "total": co["total"],
        "currency": "VND", "stock_guaranteed": hold["held"],
    }
    expires = datetime.now(UTC) + timedelta(seconds=get_settings().checkout_ttl_seconds)
    hold_expires = datetime.fromisoformat(hold["expires_at"]) if hold["held"] and hold["expires_at"] else None
    if hold_expires:
        expires = min(expires, hold_expires)  # a live checkout always has its stock held
    checkout_id = uuid.uuid4()
    with connect() as conn:
        conn.execute(
            "INSERT INTO trade.checkouts (id, agent_id, link_id, platform_id, platform_user, platform_checkout_id, "
            "items, region, proposal, proposal_hash, expires_at, hold_expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s)",
            (checkout_id, agent_id, link["link_id"], p.platform_id, link["platform_user"], co["id"],
             json.dumps(items), p.region, json.dumps(proposal), _hash(proposal), expires, hold_expires))
    return {"checkout_id": str(checkout_id), "proposal": proposal,
            "confirm_url": f"{get_settings().public_url}/v1/confirm/{checkout_id}",
            "expires_at": _iso(expires), "hold_expires_at": _iso(hold_expires)}


# -- the confirmation page (ADR-007) ------------------------------------------

def _checkout(checkout_id: str, agent_id: str | None = None) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT *, expires_at <= now() AS expired FROM trade.checkouts WHERE id = %s "
                           "AND (agent_id = %s OR %s::uuid IS NULL)", (checkout_id, agent_id, agent_id)).fetchone()
    if not row:
        raise UCPError(ErrorCode.NOT_FOUND, "checkout not found")
    return row


def start_confirmation(checkout_id: str) -> str:
    """Send the user to the platform's login. The state is single use and tied to this checkout."""
    co = _checkout(checkout_id)
    if co["expired"]:
        raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this checkout has expired")
    s, state = get_settings(), "c_" + secrets.token_urlsafe(24)
    with connect() as conn:
        conn.execute("INSERT INTO trade.confirm_states VALUES (%s, %s, now() + make_interval(secs => %s))",
                     (state, checkout_id, CONFIRM_LOGIN_TTL))
    return f"{s.mock_base_url}/oauth/authorize?" + urlencode(
        {"client_id": s.mock_client_id, "redirect_uri": f"{s.public_url}/oauth/callback", "state": state})


def _snapshot_html(co: dict) -> str:
    # Everything shown is UCP's stored snapshot, escaped: names come from the platform, never from the agent.
    p = co["proposal"]
    rows = "".join(f"<tr><td>{escape(str(i['name']))}</td><td>{i['qty']}</td><td>{i['unit_price']:,}</td></tr>"
                   for i in p["items"])
    warn = "" if p["stock_guaranteed"] else "<p><b>Stock is not guaranteed by the shop.</b></p>"
    return (f"<table border=1 cellpadding=6><tr><th>Item</th><th>Qty</th><th>Price (VND)</th></tr>{rows}</table>"
            f"<p>Subtotal {p['subtotal']:,} + shipping {p['shipping_fee']:,}<br><b>Total {p['total']:,} VND</b></p>{warn}")


def page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title>"
            f"<body style='font-family:sans-serif;max-width:30rem;margin:3rem auto'><h2>{escape(title)}</h2>{body}</body>")


def confirm_login(state: str, code: str, http: httpx.Client) -> str:
    """OAuth callback for the confirmation page: the platform account must be the one linked to the agent."""
    with connect() as conn:
        row = conn.execute("DELETE FROM trade.confirm_states WHERE state = %s AND expires_at > now() "
                           "RETURNING checkout_id", (state,)).fetchone()
    if not row:
        raise UCPError(ErrorCode.BAD_REQUEST, "unknown or expired confirmation request")
    user, _ = identity.platform_login(code, http)  # the shop's tokens are discarded, only the identity matters
    co = _checkout(str(row["checkout_id"]))
    if co["expired"]:
        raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this checkout has expired")
    if user != co["platform_user"]:
        raise UCPError(ErrorCode.FORBIDDEN, "this shop account is not the one linked to the agent")
    token = jwt.encode({"sub": str(co["id"]), "user": user, "h": co["proposal_hash"], "purpose": "confirm",
                        "exp": datetime.now(UTC) + timedelta(seconds=CONFIRM_LOGIN_TTL)},
                       get_settings().jwt_secret, algorithm="HS256")
    return page("Confirm your order", _snapshot_html(co) + (
        f"<form method=post action='/v1/confirm/{co['id']}'><input type=hidden name=token value='{escape(token)}'>"
        f"<button>Agree and confirm</button></form>"))


def record_confirmation(checkout_id: str, token: str) -> str:
    try:
        c = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise UCPError(ErrorCode.FORBIDDEN, "confirmation session expired, open the link again") from None
    if c.get("purpose") != "confirm" or c.get("sub") != checkout_id:
        raise UCPError(ErrorCode.FORBIDDEN, "confirmation does not belong to this checkout")
    co = _checkout(checkout_id)
    if co["expired"]:
        raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this checkout has expired")
    with connect() as conn:
        # bound to the proposal hash and the linked user; first confirmation wins, a resubmit of it is a no-op
        row = conn.execute(
            "UPDATE trade.checkouts SET confirmed_at = coalesce(confirmed_at, now()), confirmed_hash = %s, "
            "confirmed_user = %s WHERE id = %s AND proposal_hash = %s AND platform_user = %s "
            "AND (confirmed_at IS NULL OR (confirmed_hash = %s AND confirmed_user = %s)) RETURNING id",
            (c["h"], c["user"], checkout_id, c["h"], c["user"], c["h"], c["user"])).fetchone()
    if not row:
        raise UCPError(ErrorCode.FORBIDDEN, "the order changed since you logged in, open the link again")
    return page("Confirmed", "<p>Your order is confirmed. Go back to your assistant to finish.</p>")


# -- confirm_order ------------------------------------------------------------

def _order_by_checkout(checkout_id: str) -> dict | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM trade.orders WHERE checkout_id = %s", (checkout_id,)).fetchone()


def _place(adapter: Any, token: str, order: dict, platform_checkout_id: str, resumed: bool) -> None:
    key = order["idempotency_key"]
    try:
        plat = adapter.get_order_by_key(token, key) if resumed else None  # hard rule 5: look before resending
        if plat is None:
            try:
                plat = adapter.place_order(token, platform_checkout_id, key)
            except PlatformTimeout:
                plat = adapter.get_order_by_key(token, key)  # answer lost: ask, never resend
                if plat is None:
                    raise  # still unknown: the row stays reconciling for the order sync worker (step 7)
    except UCPError as e:
        if e.code in REFUSED:
            with connect() as conn:
                conn.execute("DELETE FROM trade.orders WHERE id = %s", (order["id"],))
        raise
    with connect() as conn:
        conn.execute("UPDATE trade.orders SET platform_order_id = %s, status = %s, payment_status = %s, "
                     "reconciling = false, total = %s, payment_url = %s, updated_at = now() WHERE id = %s",
                     (plat["id"], plat["status"], plat["payment_status"], plat["total"], plat["payment_url"],
                      order["id"]))


def confirm_order(agent_id: str, idem_key: str, raw: dict, http: httpx.Client) -> dict:
    p = parse(ConfirmOrderParams, raw)
    checkout_id = str(p.checkout_id)
    co = _checkout(checkout_id, agent_id)
    # Rechecked on every call: an unlink between checkout and confirm is refused (hard rule 11).
    link = identity.get_link(agent_id, link_id=str(co["link_id"]), http=http)
    adapter = get_adapter(co["platform_id"], http)
    order = _order_by_checkout(checkout_id)
    resumed = order is not None
    if order is None:
        if co["expired"]:
            raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this checkout has expired")
        quote = adapter.get_quote(link["access_token"], co["items"], co["region"])
        if quote["total"] > co["proposal"]["total"]:  # a lower price is applied, a higher one needs the user again
            raise UCPError(ErrorCode.PRICE_CHANGED, "the price went up since the proposal, create a new checkout",
                           {"proposal_total": co["proposal"]["total"], "current_total": quote["total"]})
        order_id = uuid.uuid4()
        with connect() as conn:
            # One transaction: use up the confirmation and open the order. Nothing the agent sends can fake it.
            used = conn.execute(
                "UPDATE trade.checkouts SET consumed_at = now() WHERE id = %s AND agent_id = %s "
                "AND confirmed_at IS NOT NULL AND consumed_at IS NULL AND confirmed_hash = proposal_hash "
                "AND confirmed_user = platform_user AND expires_at > now() RETURNING id", (checkout_id, agent_id)
            ).fetchone()
            if not used:
                raise UCPError(ErrorCode.CONFIRMATION_REQUIRED, "the user has not confirmed this checkout")
            # The shop's keys are global, so the forwarded key carries the agent id.
            order = conn.execute(
                "INSERT INTO trade.orders (id, checkout_id, agent_id, platform_id, link_id, idempotency_key) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (order_id, checkout_id, agent_id, co["platform_id"], co["link_id"], f"{agent_id}:{idem_key}")
            ).fetchone()
    if order["reconciling"]:
        _place(adapter, link["access_token"], order, co["platform_checkout_id"], resumed)
        order = _order_by_checkout(checkout_id)
    return {"order_id": str(order["id"]), "status": order["status"], "payment_url": order["payment_url"]}


def get_order(agent_id: str, order_id: str) -> dict:
    if "view_order" not in identity.permissions(agent_id):
        raise UCPError(ErrorCode.NO_USER_LINK, "link a user account first")
    with connect() as conn:
        o = conn.execute("SELECT * FROM trade.orders WHERE id = %s AND agent_id = %s", (order_id, agent_id)).fetchone()
    if not o:
        raise UCPError(ErrorCode.NOT_FOUND, "order not found")
    return {"order_id": str(o["id"]), "platform_id": o["platform_id"], "status": o["status"],
            "reconciling": o["reconciling"], "total": o["total"], "currency": "VND",
            "platform_order_ids": [o["platform_order_id"]] if o["platform_order_id"] else [],
            "created_at": _iso(o["created_at"]), "updated_at": _iso(o["updated_at"])}


# -- the one place an order's cached status changes ---------------------------------

def _path(frm: str, to: str) -> list[str]:
    """Shortest chain of allowed moves between two statuses: the cache may have missed several."""
    seen, queue = {frm}, deque([(frm, [])])
    while queue:
        cur, path = queue.popleft()
        for nxt in ORDER_TRANSITIONS.get(cur, ()):
            if nxt == to:
                return path + [nxt]
            if nxt not in seen:
                seen.add(nxt)
                queue.append((nxt, path + [nxt]))
    return []


def apply_status(conn: Any, o: dict, new: str | None = None, pay: str | None = None) -> None:
    """Move a locked order row to the platform's status, only along the transition table, and write the outbox
    event in the same transaction. The webhook and the sync worker both come through here. `o` is updated in place."""
    if new and new != o["status"]:
        for step in _path(o["status"], new):  # empty for a backwards or unknown status: ignored
            conn.execute("UPDATE trade.orders SET status = %s, updated_at = now() WHERE id = %s", (step, o["id"]))
            outbox.emit(conn, "trade", EVENT_OF.get(step, "OrderStatusChanged"), o["agent_id"],
                        {"order_id": str(o["id"]), "platform_id": o["platform_id"], "status": step,
                         "previous": o["status"]})
            o["status"] = step
    if pay and pay != o["payment_status"]:
        conn.execute("UPDATE trade.orders SET payment_status = %s, updated_at = now() WHERE id = %s", (pay, o["id"]))
        if pay == "failed":
            outbox.emit(conn, "trade", "PaymentFailed", o["agent_id"],
                        {"order_id": str(o["id"]), "platform_id": o["platform_id"]})
        o["payment_status"] = pay


# -- platform webhooks ----------------------------------------------------------

def receive_event(platform_id: str, headers: Any, body: bytes) -> None:
    if platform_id != "mock":
        raise UCPError(ErrorCode.NOT_FOUND, f"unknown platform {platform_id}")
    ts, sig, event_id = (headers.get(h, "") for h in ("x-mock-timestamp", "x-mock-signature", "x-mock-event-id"))
    expected = "sha256=" + hmac.new(get_settings().mock_webhook_secret.encode(), ts.encode() + b"." + body,
                                    hashlib.sha256).hexdigest()  # over the raw bytes, not re-serialized JSON
    if not (ts.isdigit() and event_id and hmac.compare_digest(sig, expected)):
        raise UCPError(ErrorCode.UNAUTHORIZED, "bad webhook signature")
    if abs(time.time() - int(ts)) > WEBHOOK_TOLERANCE:
        raise UCPError(ErrorCode.UNAUTHORIZED, "stale webhook")
    try:
        event = json.loads(body)
        etype, data = event["type"], event["data"]
        platform_order_id = data["order_id"]
    except (ValueError, KeyError, TypeError):
        raise UCPError(ErrorCode.BAD_REQUEST, "malformed event") from None
    with connect() as conn:
        o = conn.execute("SELECT id, agent_id, platform_id, status, payment_status FROM trade.orders "
                         "WHERE platform_id = %s AND platform_order_id = %s FOR UPDATE",
                         (platform_id, platform_order_id)).fetchone()
        if not o:
            return  # not mapped yet: not recorded, so a redelivery still counts; the sync worker (step 7) catches up
        fresh = conn.execute("INSERT INTO trade.platform_events (platform_id, event_id) VALUES (%s, %s) "
                             "ON CONFLICT DO NOTHING RETURNING 1", (platform_id, event_id)).fetchone()
        if not fresh:
            return  # duplicate delivery
        new = {"order.paid": "placed", "order.cancelled": "cancelled"}.get(etype) or (
            data.get("status") if etype == "order.status_changed" else None)
        pay = {"order.paid": "succeeded", "order.payment_failed": "failed"}.get(etype)
        apply_status(conn, o, new, pay)
