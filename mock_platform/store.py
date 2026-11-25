"""In-memory state and rules of the fake shop. No HTTP here."""

from __future__ import annotations

import hashlib
import hmac
import json
import random
import secrets
import threading
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

MODES = {"always_succeed", "always_fail", "random", "webhook_error", "slow"}

# Platform order statuses. UCP caches these, so the table is the shop's own.
ORDER_TRANSITIONS: dict[str, set[str]] = {
    "pending_payment": {"placed", "cancelled"},
    "placed": {"processing", "cancelled"},
    "processing": {"shipped", "cancelled"},
    "shipped": {"delivered"},
    "delivered": set(),
    "cancelled": set(),
}
CANCELLABLE = {"pending_payment", "placed", "processing"}

SHIPPING_FEES = {"HCM": 20_000, "HN": 25_000}
DEFAULT_SHIPPING_FEE = 35_000
# method id -> (name, surcharge on the region fee, days to arrive)
SHIPPING_METHODS = {"standard": ("Standard", 0, 3), "express": ("Express", 30_000, 1)}
# code -> (VND off the subtotal, smallest subtotal it applies to)
PROMOTIONS = {"SAVE20K": (20_000, 100_000), "SAVE100K": (100_000, 1_000_000)}


class PlatformError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


@dataclass
class Config:
    mode: str = "always_succeed"
    slow_delay: float = 3.0
    supports_holds: bool = True
    hold_seconds: int = 600
    payment_deadline_seconds: int = 1800
    webhook_url: str = ""
    webhook_secret: str = "mock-webhook-secret"
    base_url: str = "http://localhost:9000"
    seed: int | None = None


@dataclass
class Variant:
    id: str
    product_id: str
    name: str
    price: int
    stock: int  # units on hand, not counting holds
    reserved: int = 0  # units held by open checkouts

    @property
    def available(self) -> int:
        return self.stock - self.reserved


@dataclass
class Checkout:
    id: str
    user: str
    items: list[dict[str, Any]]
    region: str
    address_id: str | None
    subtotal: int
    shipping_fee: int
    total: int
    shipping_method_id: str = "standard"
    promo_code: str | None = None
    discount: int = 0
    status: str = "open"  # open | converted | released | expired
    hold_expires_at: datetime | None = None


def _default_sender(url: str) -> Callable[[dict, dict[str, str], bytes], None]:
    def send(event: dict, headers: dict[str, str], body: bytes) -> None:
        def run() -> None:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            try:
                urllib.request.urlopen(req, timeout=5).close()
            except Exception:
                pass  # the shop does not retry; UCP must reconcile

        threading.Thread(target=run, daemon=True).start()

    return send


class Platform:
    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self.reset()

    # -- lifecycle -------------------------------------------------------

    def reset(self) -> None:
        from mock_platform.seed import seed_data

        self.clock_offset = timedelta()
        self.rng = random.Random(self.config.seed)
        self.products: dict[str, dict[str, Any]] = {}
        self.variants: dict[str, Variant] = {}
        self.users: dict[str, dict[str, Any]] = {}
        self.clients: dict[str, dict[str, Any]] = {}
        seed_data(self)
        self.checkouts: dict[str, Checkout] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.order_keys: dict[str, tuple[str, str]] = {}  # key -> (order id, checkout id)
        self.cancel_keys: dict[str, str] = {}  # key -> order id
        self.refunds: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.codes: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.sender: Callable[[dict, dict[str, str], bytes], None] | None = None

    def now(self) -> datetime:
        return datetime.now(UTC) + self.clock_offset

    def advance(self, seconds: float) -> None:
        self.clock_offset += timedelta(seconds=seconds)

    @staticmethod
    def iso(dt: datetime | None) -> str | None:
        return dt.isoformat().replace("+00:00", "Z") if dt else None

    # -- webhooks --------------------------------------------------------

    def emit(self, type_: str, data: dict[str, Any], *, corrupt: bool = False) -> None:
        """Record an event and deliver it as a signed webhook if a URL is set.

        `corrupt` (or mode webhook_error) sends a bad signature on purpose."""
        event = {
            "id": "evt_" + uuid.uuid4().hex[:16],
            "type": type_,
            "created_at": self.iso(self.now()),
            "data": data,
        }
        self.events.append(event)
        sender = self.sender or (
            _default_sender(self.config.webhook_url) if self.config.webhook_url else None
        )
        if not sender:
            return
        body = json.dumps(event, separators=(",", ":")).encode()
        ts = str(int(self.now().timestamp()))
        signature = sign(self.config.webhook_secret, ts, body)
        if corrupt or self.config.mode == "webhook_error":
            signature = "sha256=" + "0" * 64
        sender(
            event,
            {
                "Content-Type": "application/json",
                "X-Mock-Event-Id": event["id"],
                "X-Mock-Timestamp": ts,
                "X-Mock-Signature": signature,
            },
            body,
        )

    # -- time based rules ------------------------------------------------

    def tick(self) -> None:
        """Apply expiry lazily: lapsed holds and unpaid orders past their deadline.

        shortcut: scans every checkout and order on each request, fine for a demo-sized
        mock, switch to a deadline heap if the mock is used for load tests."""
        now = self.now()
        for co in self.checkouts.values():
            if co.status == "open" and co.hold_expires_at and co.hold_expires_at <= now:
                self._free_hold(co)
                co.status = "expired"
        for order in list(self.orders.values()):
            if order["status"] == "pending_payment" and order["_deadline"] <= now:
                self._cancel(order, reason="payment_timeout")

    def _free_hold(self, co: Checkout) -> None:
        if co.hold_expires_at:
            for it in co.items:
                self.variants[it["variant_id"]].reserved -= it["quantity"]

    # -- catalog ---------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        return {
            "holds": self.config.supports_holds,
            "hold_seconds": self.config.hold_seconds,
            "webhooks": True,
            "hosted_confirmation_page": False,
            "cod": False,
            "partial_cancel": False,
        }

    def product_view(self, p: dict[str, Any]) -> dict[str, Any]:
        variants = [
            {"id": v.id, "name": v.name, "price": v.price, "in_stock": v.available > 0, "available": v.available}
            for v in self.variants.values()
            if v.product_id == p["id"]
        ]
        return {**p, "variants": variants}

    def search(self, q: str) -> list[dict[str, Any]]:
        q = q.strip().lower()
        return [
            self.product_view(p)
            for p in self.products.values()
            if not q or q in p["name"].lower() or q in p["description"].lower()
        ]

    def get_product(self, product_id: str) -> dict[str, Any]:
        p = self.products.get(product_id)
        if not p:
            raise PlatformError(404, "NOT_FOUND", "product not found")
        return self.product_view(p)

    def _price_lines(self, items: list[dict[str, Any]], region: str) -> tuple[list[dict], int, int]:
        if not items:
            raise PlatformError(422, "INVALID_REQUEST", "items must not be empty")
        merged: dict[str, int] = {}
        for it in items:  # same variant on two lines must be checked as one total
            merged[it["variant_id"]] = merged.get(it["variant_id"], 0) + it["quantity"]
        items = [{"variant_id": k, "quantity": q} for k, q in merged.items()]
        lines, subtotal = [], 0
        for it in items:
            v = self.variants.get(it["variant_id"])
            if not v:
                raise PlatformError(404, "NOT_FOUND", f"variant {it['variant_id']} not found")
            amount = v.price * it["quantity"]
            subtotal += amount
            lines.append({"variant_id": v.id, "name": v.name, "unit_price": v.price,
                          "quantity": it["quantity"], "amount": amount, "available": v.available})
        return lines, subtotal, SHIPPING_FEES.get(region, DEFAULT_SHIPPING_FEE)

    def shipping_options(self, region: str) -> list[dict[str, Any]]:
        base = SHIPPING_FEES.get(region, DEFAULT_SHIPPING_FEE)
        return [{"id": k, "name": name, "fee": base + extra, "eta_days": days}
                for k, (name, extra, days) in SHIPPING_METHODS.items()]

    def _price(self, items: list[dict[str, Any]], region: str, method: str | None, promo: str | None) -> dict[str, Any]:
        lines, subtotal, base = self._price_lines(items, region)
        method = method or "standard"
        if method not in SHIPPING_METHODS:
            raise PlatformError(422, "INVALID_REQUEST", f"unknown shipping method {method}")
        discount = 0
        if promo is not None:
            if promo not in PROMOTIONS:
                raise PlatformError(422, "INVALID_REQUEST", "unknown promotion code")
            discount, minimum = PROMOTIONS[promo]
            if subtotal < minimum:
                raise PlatformError(422, "INVALID_REQUEST", f"{promo} needs a subtotal of at least {minimum}")
        fee = base + SHIPPING_METHODS[method][1]
        return {"lines": lines, "subtotal": subtotal, "shipping_fee": fee, "discount": discount,
                "total": subtotal + fee - discount, "shipping_method_id": method, "promo_code": promo}

    def quote(self, items: list[dict[str, Any]], region: str, method: str | None = None,
              promo: str | None = None) -> dict[str, Any]:
        price = self._price(items, region, method, promo)
        lines = price["lines"]
        return {
            **price,
            "in_stock": all(ln["available"] >= ln["quantity"] for ln in lines),
        }

    # -- checkout --------------------------------------------------------

    def create_checkout(self, user: str, items: list[dict[str, Any]], region: str,
                        address_id: str | None, method: str | None = None,
                        promo: str | None = None) -> dict[str, Any]:
        price = self._price(items, region, method, promo)
        lines = price["lines"]
        for ln in lines:
            if ln["available"] < ln["quantity"]:
                raise PlatformError(409, "OUT_OF_STOCK", f"{ln['variant_id']}: only {ln['available']} left")
        co = Checkout(
            id="co_" + uuid.uuid4().hex[:12], user=user,
            items=[{"variant_id": ln["variant_id"], "quantity": ln["quantity"]} for ln in lines],
            region=region, address_id=address_id,
            subtotal=price["subtotal"], shipping_fee=price["shipping_fee"], total=price["total"],
            shipping_method_id=price["shipping_method_id"], promo_code=promo, discount=price["discount"],
        )
        if self.config.supports_holds:
            co.hold_expires_at = self.now() + timedelta(seconds=self.config.hold_seconds)
            for it in co.items:
                self.variants[it["variant_id"]].reserved += it["quantity"]
        self.checkouts[co.id] = co
        return self.checkout_view(co)

    def checkout_view(self, co: Checkout) -> dict[str, Any]:
        lines, _, _ = self._price_lines(co.items, co.region)
        return {
            "id": co.id, "status": co.status, "lines": lines, "subtotal": co.subtotal,
            "shipping_fee": co.shipping_fee, "discount": co.discount, "total": co.total, "region": co.region,
            "shipping_method_id": co.shipping_method_id, "promo_code": co.promo_code,
            "address_id": co.address_id,
            "hold": {"held": co.hold_expires_at is not None and co.status == "open",
                     "expires_at": self.iso(co.hold_expires_at)},
        }

    def get_checkout(self, user: str, checkout_id: str) -> Checkout:
        co = self.checkouts.get(checkout_id)
        if not co or co.user != user:
            raise PlatformError(404, "NOT_FOUND", "checkout not found")
        return co

    def renew_hold(self, user: str, checkout_id: str) -> dict[str, Any]:
        co = self.get_checkout(user, checkout_id)
        if not self.config.supports_holds:
            raise PlatformError(409, "HOLDS_NOT_SUPPORTED", "this platform does not hold stock")
        if co.status != "open":
            raise PlatformError(409, "CHECKOUT_EXPIRED", f"checkout is {co.status}")
        co.hold_expires_at = self.now() + timedelta(seconds=self.config.hold_seconds)
        return self.checkout_view(co)

    def release_hold(self, user: str, checkout_id: str) -> dict[str, Any]:
        co = self.get_checkout(user, checkout_id)
        if co.status == "open":
            self._free_hold(co)
            co.status = "released"
        return self.checkout_view(co)

    # -- orders ----------------------------------------------------------

    def place_order(self, user: str, checkout_id: str, key: str) -> dict[str, Any]:
        if key in self.order_keys:
            order_id, used_for = self.order_keys[key]
            if used_for != checkout_id:
                raise PlatformError(422, "IDEMPOTENCY_CONFLICT", "key already used for another checkout")
            return self.order_view(self.get_order(user, order_id))
        co = self.get_checkout(user, checkout_id)
        if co.status != "open":
            raise PlatformError(409, "CHECKOUT_EXPIRED", f"checkout is {co.status}")
        held = co.hold_expires_at is not None
        # Price is read now, not from the checkout: it may have moved since.
        price = self._price(co.items, co.region, co.shipping_method_id, co.promo_code)
        lines = price["lines"]
        for ln in lines:
            v = self.variants[ln["variant_id"]]
            have = v.stock if held else v.available  # a hold already counts in `reserved`
            if have < ln["quantity"]:
                raise PlatformError(409, "OUT_OF_STOCK", f"{v.id}: only {max(have, 0)} left")
        for ln in lines:
            v = self.variants[ln["variant_id"]]
            if held:
                v.reserved -= ln["quantity"]
            v.stock -= ln["quantity"]
        co.status = "converted"
        order = {
            "id": "ord_" + uuid.uuid4().hex[:12], "user": user, "checkout_id": co.id,
            "status": "pending_payment", "payment_status": "pending",
            "lines": lines, "subtotal": price["subtotal"], "shipping_fee": price["shipping_fee"],
            "discount": price["discount"], "total": price["total"],
            "idempotency_key": key, "refund_ref": None,
            "created_at": self.iso(self.now()),
            "_deadline": self.now() + timedelta(seconds=self.config.payment_deadline_seconds),
            "_items": co.items,
        }
        self.orders[order["id"]] = order
        self.order_keys[key] = (order["id"], co.id)
        return self.order_view(order)

    def order_view(self, o: dict[str, Any]) -> dict[str, Any]:
        view = {k: v for k, v in o.items() if not k.startswith("_") and k != "user"}
        view["payment_deadline"] = self.iso(o["_deadline"])
        view["payment_url"] = f"{self.config.base_url}/pay/{o['id']}" if o["status"] == "pending_payment" else None
        return view

    def get_order(self, user: str, order_id: str) -> dict[str, Any]:
        o = self.orders.get(order_id)
        if not o or o["user"] != user:
            raise PlatformError(404, "NOT_FOUND", "order not found")
        return o

    def order_by_key(self, user: str, key: str) -> dict[str, Any]:
        if key not in self.order_keys:
            raise PlatformError(404, "NOT_FOUND", "no order for this key")
        return self.get_order(user, self.order_keys[key][0])

    # -- payment ---------------------------------------------------------

    def pay(self, order_id: str, outcome: str) -> dict[str, Any]:
        """outcome: 'auto' lets the test mode decide, 'fail' is the user declining."""
        o = self.orders.get(order_id)
        if not o:
            raise PlatformError(404, "NOT_FOUND", "order not found")
        if o["status"] != "pending_payment":
            raise PlatformError(409, "INVALID_STATE", f"order is {o['status']}")
        mode = self.config.mode
        ok = outcome == "auto" and (
            mode != "always_fail" and (mode != "random" or self.rng.random() < 0.5)
        )
        if ok:
            o["payment_status"] = "succeeded"
            self._set_status(o, "placed", quiet=True)
            self.emit("order.paid", {"order_id": o["id"], "status": "placed", "total": o["total"],
                                     "idempotency_key": o["idempotency_key"]})
        else:
            o["payment_status"] = "failed"
            self.emit("order.payment_failed", {"order_id": o["id"], "status": o["status"]})
        return self.order_view(o)

    def _set_status(self, o: dict[str, Any], new: str, quiet: bool = False) -> None:
        if new not in ORDER_TRANSITIONS[o["status"]]:
            raise PlatformError(409, "INVALID_STATE", f"{o['status']} -> {new} not allowed")
        prev, o["status"] = o["status"], new
        if not quiet:
            self.emit("order.status_changed", {"order_id": o["id"], "status": new, "previous": prev})

    def advance_order(self, order_id: str, status: str) -> dict[str, Any]:
        o = self.orders.get(order_id)
        if not o:
            raise PlatformError(404, "NOT_FOUND", "order not found")
        if status == "cancelled":
            self._cancel(o, reason="platform")
        else:
            self._set_status(o, status)
        return self.order_view(o)

    # -- cancel and refund -----------------------------------------------

    def cancel_order(self, user: str, order_id: str, key: str) -> dict[str, Any]:
        if key in self.cancel_keys:
            if self.cancel_keys[key] != order_id:
                raise PlatformError(422, "IDEMPOTENCY_CONFLICT", "key already used for another order")
            o = self.get_order(user, order_id)
            return {"order": self.order_view(o), "refund_ref": o["refund_ref"]}
        o = self.get_order(user, order_id)
        if o["status"] not in CANCELLABLE:
            raise PlatformError(409, "CANNOT_CANCEL", f"order is {o['status']}")
        self._cancel(o, reason="user")
        self.cancel_keys[key] = order_id
        return {"order": self.order_view(o), "refund_ref": o["refund_ref"]}

    def _cancel(self, o: dict[str, Any], reason: str) -> None:
        self._set_status(o, "cancelled", quiet=True)
        for it in o["_items"]:
            self.variants[it["variant_id"]].stock += it["quantity"]
        if o["payment_status"] == "succeeded":
            ref = "rf_" + uuid.uuid4().hex[:12]
            self.refunds[ref] = {"ref": ref, "order_id": o["id"], "amount": o["total"], "status": "initiated"}
            o["refund_ref"] = ref
        self.emit("order.cancelled", {"order_id": o["id"], "reason": reason, "refund_ref": o["refund_ref"]})

    def get_refund(self, ref: str) -> dict[str, Any]:
        r = self.refunds.get(ref)
        if not r:
            raise PlatformError(404, "NOT_FOUND", "refund not found")
        return r

    def settle_refund(self, ref: str, ok: bool) -> dict[str, Any]:
        r = self.get_refund(ref)
        if r["status"] != "initiated":
            raise PlatformError(409, "INVALID_STATE", f"refund is {r['status']}")
        r["status"] = "completed" if ok else "failed"
        self.emit("refund.completed" if ok else "refund.failed", {"refund_ref": ref, "order_id": r["order_id"]})
        return r

    # -- oauth -----------------------------------------------------------

    def login(self, username: str, password: str) -> str | None:
        u = self.users.get(username)
        return username if u and secrets.compare_digest(u["password"], password) else None

    def issue_code(self, user: str, client_id: str, redirect_uri: str) -> str:
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"user": user, "client_id": client_id, "redirect_uri": redirect_uri,
                            "expires": self.now() + timedelta(minutes=5)}
        return code

    def _issue_tokens(self, user: str, client_id: str) -> dict[str, Any]:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.tokens[access] = {"user": user, "expires": self.now() + timedelta(hours=1)}
        self.refresh_tokens[refresh] = {"user": user, "client_id": client_id}
        return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer", "expires_in": 3600}

    def exchange(self, form: dict[str, str]) -> dict[str, Any]:
        client = self.clients.get(form.get("client_id", ""))
        if not client or not secrets.compare_digest(client["secret"], form.get("client_secret", "")):
            raise PlatformError(401, "INVALID_CLIENT", "bad client credentials")
        grant = form.get("grant_type")
        if grant == "authorization_code":
            c = self.codes.pop(form.get("code", ""), None)  # single use
            if (not c or c["expires"] <= self.now() or c["client_id"] != client["id"]
                    or c["redirect_uri"] != form.get("redirect_uri")):
                raise PlatformError(400, "INVALID_GRANT", "bad or expired code")
            return self._issue_tokens(c["user"], client["id"])
        if grant == "refresh_token":
            r = self.refresh_tokens.pop(form.get("refresh_token", ""), None)  # rotated: old one dies
            if not r or r["client_id"] != client["id"]:
                raise PlatformError(400, "INVALID_GRANT", "bad refresh token")
            return self._issue_tokens(r["user"], client["id"])
        raise PlatformError(400, "UNSUPPORTED_GRANT", "grant_type must be authorization_code or refresh_token")

    def user_for_token(self, token: str | None) -> str:
        t = self.tokens.get(token or "")
        if not t or t["expires"] <= self.now():
            raise PlatformError(401, "UNAUTHORIZED", "missing or expired access token")
        return t["user"]
