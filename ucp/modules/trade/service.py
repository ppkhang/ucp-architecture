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


class CheckoutRef(BaseModel):
    checkout_id: uuid.UUID


class ShippingParams(CheckoutRef):
    shipping_method_id: str = Field(min_length=1, max_length=64)


class PromotionParams(CheckoutRef):
    code: str = Field(min_length=1, max_length=64)


def parse(model: type[BaseModel], params: dict) -> Any:
    try:
        return model.model_validate(params)
    except ValidationError as e:
        raise UCPError(ErrorCode.BAD_REQUEST, "invalid params",
                       {"errors": [{"loc": list(x["loc"]), "msg": x["msg"]} for x in e.errors()]}) from None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def snapshot_hash(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# -- create_checkout ---------------------------------------------------------

def create_checkout(agent_id: str, raw: dict, http: httpx.Client) -> dict:
    p = parse(CreateCheckoutParams, raw)
    adapter = get_adapter(p.platform_id, http)
    link = identity.get_link(agent_id, platform_id=p.platform_id, http=http)  # create_order needs a UserLink
    with connect() as conn:
        _check_open_limit(conn, p.platform_id, link["platform_user"])  # fail fast, before the shop holds stock
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
    names = {vid: {"product_id": pid, "name": name} for vid, (pid, name) in meta.items()}
    proposal = _proposal(co, names)
    expires = datetime.now(UTC) + timedelta(seconds=get_settings().checkout_ttl_seconds)
    hold_expires = _hold_expiry(co)
    if hold_expires:
        expires = min(expires, hold_expires)  # a live checkout always has its stock held
    checkout_id = uuid.uuid4()
    try:
        with connect() as conn:
            # BR-26 again, serialized per shop user, so two agents cannot both take the last slot
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 1))",
                         (f"{p.platform_id}:{link['platform_user']}",))
            _check_open_limit(conn, p.platform_id, link["platform_user"])
            conn.execute(
                "INSERT INTO trade.checkouts (id, agent_id, link_id, platform_id, platform_user, platform_checkout_id, "
                "items, region, proposal, proposal_hash, expires_at, hold_expires_at, shipping_method_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s, %s, %s)",
                (checkout_id, agent_id, link["link_id"], p.platform_id, link["platform_user"], co["id"],
                 json.dumps(items), p.region, json.dumps(proposal), snapshot_hash(proposal), expires, hold_expires,
                 proposal["shipping_method_id"]))
    except UCPError:
        _release(adapter, link["access_token"], co["id"])
        raise
    return _checkout_result(checkout_id, proposal, expires, hold_expires)


def _proposal(co: dict, names: dict[str, dict]) -> dict:
    """UCP's snapshot of what the platform quoted. Every amount is the platform's own (ADR-006)."""
    return {
        "items": [{"product_id": names[ln["variant_id"]]["product_id"], "variant_id": ln["variant_id"],
                   "name": names[ln["variant_id"]]["name"], "qty": ln["quantity"], "unit_price": ln["unit_price"]}
                  for ln in co["lines"]],
        "subtotal": co["subtotal"], "shipping_fee": co["shipping_fee"], "discount": co["discount"],
        "total": co["total"], "currency": "VND", "stock_guaranteed": co["hold"]["held"],
        "shipping_method_id": co["shipping_method_id"],
    }


def _hold_expiry(co: dict) -> datetime | None:
    hold = co["hold"]
    return datetime.fromisoformat(hold["expires_at"]) if hold["held"] and hold["expires_at"] else None


def _checkout_result(checkout_id: Any, proposal: dict, expires: datetime, hold_expires: datetime | None) -> dict:
    return {"checkout_id": str(checkout_id), "proposal": proposal,
            "confirm_url": f"{get_settings().public_url}/v1/confirm/{checkout_id}",
            "expires_at": _iso(expires), "hold_expires_at": _iso(hold_expires)}


def _release(adapter: Any, token: str, platform_checkout_id: str) -> None:
    try:  # best effort: a hold that is not released still lapses at the platform
        adapter.release_hold(token, platform_checkout_id)
    except UCPError:
        pass


# -- BR-26: limit open checkouts per shop user, across all agents ---------------

def _check_open_limit(conn: Any, platform_id: str, platform_user: str) -> None:
    s = get_settings()
    mine = (platform_id, platform_user)
    n = conn.execute("SELECT count(*) AS n FROM trade.checkouts WHERE platform_id = %s AND platform_user = %s "
                     "AND status IN ('open', 'updating') AND consumed_at IS NULL AND expires_at > now()",
                     mine).fetchone()["n"]
    if n >= s.max_open_checkouts:
        raise UCPError(ErrorCode.QUOTA_EXCEEDED, "too many open checkouts for this shop account",
                       {"max_open_checkouts": s.max_open_checkouts})
    # the last checkouts that ended: ordered, cancelled or failed (expires_at is set to that moment), or expired
    last = conn.execute(
        "SELECT consumed_at IS NULL AND status IN ('expired', 'open', 'updating') AS lapsed, "
        "coalesce(consumed_at, expires_at) > now() - make_interval(secs => %s) AS recent FROM trade.checkouts "
        "WHERE platform_id = %s AND platform_user = %s AND (consumed_at IS NOT NULL OR expires_at <= now()) "
        "ORDER BY coalesce(consumed_at, expires_at) DESC LIMIT %s",
        (s.checkout_cooldown_seconds, *mine, s.checkout_cooldown_after)).fetchall()
    if len(last) == s.checkout_cooldown_after and all(r["lapsed"] for r in last) and last[0]["recent"]:
        raise UCPError(ErrorCode.QUOTA_EXCEEDED, "several checkouts in a row expired, wait before starting another",
                       {"cooldown_seconds": s.checkout_cooldown_seconds})


# -- changing an open checkout: shipping, promotion, extend, cancel --------------

def _closed(co: dict) -> UCPError:
    """Why a checkout can no longer be used."""
    if co["failure_code"]:
        return UCPError(ErrorCode(co["failure_code"]), "the shop refused this order, create a new checkout")
    if co["consumed_at"]:
        return UCPError(ErrorCode.BAD_REQUEST, "this checkout was already ordered")
    if co["status"] == "updating" and not co["expired"]:
        return UCPError(ErrorCode.BAD_REQUEST, "this checkout is being changed, try again")
    reason = "was cancelled" if co["status"] == "cancelled" else "has expired"
    return UCPError(ErrorCode.CHECKOUT_EXPIRED, f"this checkout {reason}")


def _require_open(co: dict) -> None:
    if co["status"] != "open" or co["expired"] or co["consumed_at"]:
        raise _closed(co)


def _claim_open(checkout_id: str, agent_id: str, http: httpx.Client) -> tuple[dict, dict, Any]:
    """Take an open checkout for a change. While it is 'updating' it cannot be confirmed, ordered or changed
    again, and no transaction stays open across the platform calls. The ExpiryWorker clears a stuck one."""
    co = _checkout(checkout_id, agent_id)
    link = identity.get_link(agent_id, link_id=str(co["link_id"]), http=http)
    with connect() as conn:
        claimed = conn.execute(
            "UPDATE trade.checkouts SET status = 'updating' WHERE id = %s AND agent_id = %s AND status = 'open' "
            "AND consumed_at IS NULL AND expires_at > now() RETURNING *", (checkout_id, agent_id)).fetchone()
    if not claimed:
        raise _closed(_checkout(checkout_id, agent_id))
    return claimed, link, get_adapter(claimed["platform_id"], http)


def _unclaim(checkout_id: Any) -> None:
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET status = 'open' WHERE id = %s AND status = 'updating'", (checkout_id,))


def _change_basket(co: dict, link: dict, adapter: Any, method: str | None, promo: str | None) -> dict:
    """The platform prices a checkout when it makes it, so a change means a new platform checkout and a new
    snapshot. The user's confirmation was for the old snapshot, so it is dropped."""
    token = link["access_token"]
    try:
        adapter.get_quote(token, co["items"], co["region"], method, promo)  # a bad input fails before the hold goes
    except BaseException:
        _unclaim(co["id"])
        raise
    _release(adapter, token, co["platform_checkout_id"])  # first, or the same units would need to be held twice
    try:
        new = adapter.create_checkout(token, co["items"], co["region"], method, promo)
    except BaseException:  # the old hold is gone, so this checkout cannot be ordered any more
        with connect() as conn:
            conn.execute("UPDATE trade.checkouts SET status = 'cancelled', expires_at = now() "
                         "WHERE id = %s AND status = 'updating'", (co["id"],))
            outbox.emit(conn, "trade", "CheckoutCancelled", co["agent_id"],
                        {"checkout_id": str(co["id"]), "platform_id": co["platform_id"], "reason": "change_failed"})
        raise
    proposal = _proposal(new, {i["variant_id"]: i for i in co["proposal"]["items"]})
    hold_expires = _hold_expiry(new)
    expires = min(co["expires_at"], hold_expires) if hold_expires else co["expires_at"]  # never a free extension
    with connect() as conn:
        saved = conn.execute(
            "UPDATE trade.checkouts SET status = 'open', platform_checkout_id = %s, proposal = %s::jsonb, "
            "proposal_hash = %s, expires_at = %s, hold_expires_at = %s, shipping_method_id = %s, promo_code = %s, "
            "confirmed_at = NULL, confirmed_hash = NULL, confirmed_user = NULL "
            "WHERE id = %s AND status = 'updating' RETURNING id",
            (new["id"], json.dumps(proposal), snapshot_hash(proposal), expires, hold_expires, method, promo,
             co["id"])).fetchone()
    if not saved:  # it expired while the shop was answering
        _release(adapter, token, new["id"])
        raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this checkout has expired")
    return _checkout_result(co["id"], proposal, expires, hold_expires)


def select_shipping_method(agent_id: str, raw: dict, http: httpx.Client) -> dict:
    p = parse(ShippingParams, raw)
    co, link, adapter = _claim_open(str(p.checkout_id), agent_id, http)
    result = _change_basket(co, link, adapter, p.shipping_method_id, co["promo_code"])
    with connect() as conn:
        outbox.emit(conn, "trade", "ShippingMethodSelected", agent_id,
                    {"checkout_id": result["checkout_id"], "platform_id": co["platform_id"],
                     "shipping_method_id": p.shipping_method_id})
    return result


def apply_promotion(agent_id: str, raw: dict, http: httpx.Client) -> dict:
    p = parse(PromotionParams, raw)
    co, link, adapter = _claim_open(str(p.checkout_id), agent_id, http)
    return _change_basket(co, link, adapter, co["shipping_method_id"], p.code)


def extend_checkout(agent_id: str, raw: dict, http: httpx.Client) -> dict:
    """Once per checkout, and never past the platform's hold (R-12)."""
    p = parse(CheckoutRef, raw)
    co, link, adapter = _claim_open(str(p.checkout_id), agent_id, http)
    try:
        if co["extended_at"]:
            raise UCPError(ErrorCode.BAD_REQUEST, "this checkout was already extended once")
        expires = datetime.now(UTC) + timedelta(seconds=get_settings().checkout_ttl_seconds)
        hold_expires = None
        if co["hold_expires_at"]:
            hold_expires = _hold_expiry(adapter.renew_hold(link["access_token"], co["platform_checkout_id"]))
            if not hold_expires:
                raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "the shop no longer holds this checkout")
            expires = min(expires, hold_expires)
        with connect() as conn:
            saved = conn.execute(
                "UPDATE trade.checkouts SET status = 'open', extended_at = now(), expires_at = %s, "
                "hold_expires_at = %s WHERE id = %s AND status = 'updating' RETURNING id",
                (expires, hold_expires, co["id"])).fetchone()
            if not saved:
                raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this checkout has expired")
            outbox.emit(conn, "trade", "CheckoutExtended", agent_id,
                        {"checkout_id": str(co["id"]), "platform_id": co["platform_id"],
                         "new_expires_at": _iso(expires)})
    except BaseException:
        _unclaim(co["id"])
        raise
    return _checkout_result(co["id"], co["proposal"], expires, hold_expires)


def cancel_checkout(agent_id: str, raw: dict, http: httpx.Client) -> dict:
    p = parse(CheckoutRef, raw)
    co, link, adapter = _claim_open(str(p.checkout_id), agent_id, http)
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET status = 'cancelled', expires_at = now() "
                     "WHERE id = %s AND status = 'updating'", (co["id"],))
        outbox.emit(conn, "trade", "CheckoutCancelled", agent_id,
                    {"checkout_id": str(co["id"]), "platform_id": co["platform_id"], "reason": "agent"})
    _release(adapter, link["access_token"], co["platform_checkout_id"])
    return {"checkout_id": str(co["id"]), "status": "cancelled"}


def shipping_options(agent_id: str, platform_id: str, region: str, http: httpx.Client) -> dict:
    return {"options": get_adapter(platform_id, http).shipping_options(region)}


# -- the confirmation page (ADR-007) ------------------------------------------

def _checkout(checkout_id: str, agent_id: str | None = None) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT *, expires_at <= now() AS expired FROM trade.checkouts WHERE id = %s "
                           "AND (agent_id = %s OR %s::uuid IS NULL)", (checkout_id, agent_id, agent_id)).fetchone()
    if not row:
        raise UCPError(ErrorCode.NOT_FOUND, "checkout not found")
    return row


def _login_url(checkout_id: str | None = None, cancel_id: str | None = None) -> str:
    """Send the user to the platform's login. The state is single use and tied to one checkout or cancel request."""
    s, state = get_settings(), "c_" + secrets.token_urlsafe(24)
    with connect() as conn:
        conn.execute("INSERT INTO trade.confirm_states (state, checkout_id, cancel_id, expires_at) "
                     "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
                     (state, checkout_id, cancel_id, CONFIRM_LOGIN_TTL))
    return f"{s.mock_base_url}/oauth/authorize?" + urlencode(
        {"client_id": s.mock_client_id, "redirect_uri": f"{s.public_url}/oauth/callback", "state": state})


def start_confirmation(proposal_id: str) -> str:
    """One page for both money steps (ADR-007): the id names a checkout or a cancel request."""
    try:
        co = _checkout(proposal_id)
    except UCPError:
        _require_pending_cancel(_cancel_request(proposal_id))
        return _login_url(cancel_id=proposal_id)
    _require_open(co)
    return _login_url(checkout_id=proposal_id)


def _snapshot_html(co: dict) -> str:
    # Everything shown is UCP's stored snapshot, escaped: names come from the platform, never from the agent.
    p = co["proposal"]
    rows = "".join(f"<tr><td>{escape(str(i['name']))}</td><td>{i['qty']}</td><td>{i['unit_price']:,}</td></tr>"
                   for i in p["items"])
    off = f" - discount {p['discount']:,}" if p["discount"] else ""
    method = escape(str(p.get("shipping_method_id") or "standard"))
    warn = "" if p["stock_guaranteed"] else "<p><b>Stock is not guaranteed by the shop.</b></p>"
    return (f"<table border=1 cellpadding=6><tr><th>Item</th><th>Qty</th><th>Price (VND)</th></tr>{rows}</table>"
            f"<p>Subtotal {p['subtotal']:,} + shipping ({method}) {p['shipping_fee']:,}{off}"
            f"<br><b>Total {p['total']:,} VND</b></p>{warn}")


def page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><title>{escape(title)}</title>"
            f"<body style='font-family:sans-serif;max-width:30rem;margin:3rem auto'><h2>{escape(title)}</h2>{body}</body>")


def _sign_confirmation(purpose: str, subject: Any, user: str, snapshot_hash: str) -> str:
    return jwt.encode({"sub": str(subject), "user": user, "h": snapshot_hash, "purpose": purpose,
                       "exp": datetime.now(UTC) + timedelta(seconds=CONFIRM_LOGIN_TTL)},
                      get_settings().jwt_secret, algorithm="HS256")


def _agree_form(subject: Any, token: str, label: str) -> str:
    return (f"<form method=post action='/v1/confirm/{subject}'><input type=hidden name=token value='{escape(token)}'>"
            f"<button>{label}</button></form>")


def confirm_login(state: str, code: str, http: httpx.Client) -> str:
    """OAuth callback for the confirmation page: the platform account must be the one linked to the agent."""
    with connect() as conn:
        row = conn.execute("DELETE FROM trade.confirm_states WHERE state = %s AND expires_at > now() "
                           "RETURNING checkout_id, cancel_id", (state,)).fetchone()
    if not row:
        raise UCPError(ErrorCode.BAD_REQUEST, "unknown or expired confirmation request")
    user, _ = identity.platform_login(code, http)  # the shop's tokens are discarded, only the identity matters
    wrong_user = UCPError(ErrorCode.FORBIDDEN, "this shop account is not the one linked to the agent")
    if row["cancel_id"]:
        req = _cancel_request(str(row["cancel_id"]))
        _require_pending_cancel(req)
        if user != req["platform_user"]:
            raise wrong_user
        token = _sign_confirmation("cancel", req["id"], user, req["snapshot_hash"])
        return page("Cancel your order", _cancel_html(req) + _agree_form(req["id"], token, "Agree and cancel the order"))
    co = _checkout(str(row["checkout_id"]))
    _require_open(co)
    if user != co["platform_user"]:
        raise wrong_user
    token = _sign_confirmation("confirm", co["id"], user, co["proposal_hash"])
    return page("Confirm your order", _snapshot_html(co) + _agree_form(co["id"], token, "Agree and confirm"))


def record_confirmation(proposal_id: str, token: str) -> str:
    try:
        c = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        raise UCPError(ErrorCode.FORBIDDEN, "confirmation session expired, open the link again") from None
    # the purpose keeps the two apart: agreeing to buy can never count as agreeing to cancel
    if c.get("purpose") not in ("confirm", "cancel") or c.get("sub") != proposal_id:
        raise UCPError(ErrorCode.FORBIDDEN, "confirmation does not belong to this request")
    if c["purpose"] == "cancel":
        return _record_cancel(proposal_id, c)
    co = _checkout(proposal_id)
    if not co["consumed_at"]:  # a resubmit after the order went through stays harmless
        _require_open(co)
    with connect() as conn:
        # bound to the proposal hash and the linked user; first confirmation wins, a resubmit of it is a no-op
        row = conn.execute(
            "UPDATE trade.checkouts SET confirmed_at = coalesce(confirmed_at, now()), confirmed_hash = %s, "
            "confirmed_user = %s WHERE id = %s AND status = 'open' AND proposal_hash = %s AND platform_user = %s "
            "AND (confirmed_at IS NULL OR (confirmed_hash = %s AND confirmed_user = %s)) RETURNING id",
            (c["h"], c["user"], proposal_id, c["h"], c["user"], c["h"], c["user"])).fetchone()
    if not row:
        raise UCPError(ErrorCode.FORBIDDEN, "the order changed since you logged in, open the link again")
    return page("Confirmed", "<p>Your order is confirmed. Go back to your assistant to finish.</p>")


# -- cancel requests, confirmed on the same page ---------------------------------

def _cancel_request(request_id: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT *, expires_at <= now() AS expired FROM trade.cancel_requests WHERE id = %s",
                           (request_id,)).fetchone()
    if not row:
        raise UCPError(ErrorCode.NOT_FOUND, "checkout not found")
    return row


def _require_pending_cancel(req: dict) -> None:
    if req["expired"] or req["idempotency_key"]:
        raise UCPError(ErrorCode.CHECKOUT_EXPIRED, "this cancel link has expired, ask your assistant for a new one")


def _cancel_html(req: dict) -> str:
    # UCP's stored snapshot only: nothing the agent sent, including its reason, is shown (hard rule 3)
    s = req["snapshot"]
    rows = "".join(f"<li>{escape(str(i['name']))} x {i['qty']}</li>" for i in s["items"])
    return (f"<p>Order {escape(str(s['platform_order_id']))}</p><ul>{rows}</ul>"
            f"<p><b>Total {s['total']:,} VND</b></p><p>The shop decides whether it can still be cancelled.</p>")


def _record_cancel(request_id: str, c: dict) -> str:
    req = _cancel_request(request_id)
    if not req["idempotency_key"]:
        _require_pending_cancel(req)
    with connect() as conn:
        row = conn.execute(
            "UPDATE trade.cancel_requests SET confirmed_at = coalesce(confirmed_at, now()), confirmed_hash = %s, "
            "confirmed_user = %s WHERE id = %s AND snapshot_hash = %s AND platform_user = %s RETURNING id",
            (c["h"], c["user"], request_id, c["h"], c["user"])).fetchone()
    if not row:
        raise UCPError(ErrorCode.FORBIDDEN, "this does not match the cancel request")
    return page("Confirmed", "<p>You agreed to cancel. Go back to your assistant to finish.</p>")


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
                gone = conn.execute("DELETE FROM trade.orders WHERE id = %s RETURNING checkout_id, agent_id, platform_id",
                                    (order["id"],)).fetchone()
                if gone:
                    conn.execute("UPDATE trade.checkouts SET status = 'failed', failure_code = %s, expires_at = now() "
                                 "WHERE id = %s", (e.code.value, gone["checkout_id"]))
                    outbox.emit(conn, "trade", "CheckoutCancelled", gone["agent_id"],
                                {"checkout_id": str(gone["checkout_id"]), "platform_id": gone["platform_id"],
                                 "reason": e.code.value})
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
        _require_open(co)  # a refused order answers with the shop's reason
        quote = adapter.get_quote(link["access_token"], co["items"], co["region"], co["shipping_method_id"],
                                  co["promo_code"])
        if quote["total"] > co["proposal"]["total"]:  # a lower price is applied, a higher one needs the user again
            raise UCPError(ErrorCode.PRICE_CHANGED, "the price went up since the proposal, create a new checkout",
                           {"proposal_total": co["proposal"]["total"], "current_total": quote["total"]})
        order_id = uuid.uuid4()
        with connect() as conn:
            # One transaction: use up the confirmation and open the order. Nothing the agent sends can fake it.
            used = conn.execute(
                "UPDATE trade.checkouts SET consumed_at = now() WHERE id = %s AND agent_id = %s "
                "AND confirmed_at IS NOT NULL AND consumed_at IS NULL AND confirmed_hash = proposal_hash "
                "AND confirmed_user = platform_user AND status = 'open' AND proposal_hash = %s AND expires_at > now() "
                "RETURNING id", (checkout_id, agent_id, co["proposal_hash"])  # the snapshot whose price we just checked
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


def save_refund_ref(conn: Any, order_id: Any, ref: str | None) -> None:
    if ref:
        conn.execute("UPDATE trade.orders SET refund_ref = %s WHERE id = %s", (ref, order_id))


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
        if etype == "order.cancelled":
            save_refund_ref(conn, o["id"], data.get("refund_ref"))
