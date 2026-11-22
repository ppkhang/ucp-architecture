"""The fake shop: a separate FastAPI app that UCP's MockPlatformAdapter talks to."""

from __future__ import annotations

import asyncio
import html
import json
import os
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from mock_platform.store import MODES, Config, Platform, PlatformError


class Item(BaseModel):
    variant_id: str
    quantity: int = Field(ge=1, le=100)


class QuoteIn(BaseModel):
    items: list[Item]
    region: str = "HCM"


class CheckoutIn(QuoteIn):
    address_id: str | None = None


class OrderIn(BaseModel):
    checkout_id: str


class ConfigIn(BaseModel):
    mode: str | None = None
    slow_delay: float | None = Field(None, ge=0)
    supports_holds: bool | None = None
    hold_seconds: int | None = Field(None, ge=1)
    payment_deadline_seconds: int | None = Field(None, ge=1)
    webhook_url: str | None = None
    webhook_secret: str | None = None
    seed: int | None = None


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>"
        f"<body style='font-family:sans-serif;max-width:28rem;margin:3rem auto'>"
        f"<h2>{html.escape(title)}</h2>{body}</body>",
        status_code=status,
    )


def create_app(config: Config | None = None) -> FastAPI:
    if config is None:
        config = Config(
            mode=os.getenv("MOCK_MODE", "always_succeed"),
            base_url=os.getenv("MOCK_BASE_URL", "http://localhost:9000"),
            webhook_url=os.getenv("MOCK_WEBHOOK_URL", ""),
            webhook_secret=os.getenv("MOCK_WEBHOOK_SECRET", "mock-webhook-secret"),
        )
    platform = Platform(config)
    app = FastAPI(title="UCP mock platform", version="0.1.0")
    app.state.platform = platform

    @app.exception_handler(PlatformError)
    async def on_error(_: Request, exc: PlatformError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.middleware("http")
    async def behaviour(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/_admin") and path != "/health":
            if platform.config.mode == "slow":
                await asyncio.sleep(platform.config.slow_delay)
            platform.tick()
        return await call_next(request)

    def user_of(authorization: str | None) -> str:
        token = authorization[7:] if authorization and authorization.lower().startswith("bearer ") else None
        return platform.user_for_token(token)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # -- catalog and quote (public) ---------------------------------------

    @app.get("/capabilities")
    async def capabilities():
        return platform.capabilities()

    @app.get("/products")
    async def products(q: str = ""):
        return {"items": platform.search(q)}

    @app.get("/products/{product_id}")
    async def product(product_id: str):
        return platform.get_product(product_id)

    @app.post("/quote")
    async def quote(body: QuoteIn):
        return platform.quote([i.model_dump() for i in body.items], body.region)

    # -- checkout and orders (user token) ---------------------------------

    @app.post("/checkouts", status_code=201)
    async def create_checkout(body: CheckoutIn, authorization: str | None = Header(None)):
        user = user_of(authorization)
        return platform.create_checkout(user, [i.model_dump() for i in body.items], body.region, body.address_id)

    @app.post("/checkouts/{checkout_id}/renew")
    async def renew(checkout_id: str, authorization: str | None = Header(None)):
        return platform.renew_hold(user_of(authorization), checkout_id)

    @app.post("/checkouts/{checkout_id}/release")
    async def release(checkout_id: str, authorization: str | None = Header(None)):
        return platform.release_hold(user_of(authorization), checkout_id)

    @app.post("/orders", status_code=201)
    async def place_order(body: OrderIn, authorization: str | None = Header(None),
                          idempotency_key: str | None = Header(None)):
        user = user_of(authorization)
        if not idempotency_key:
            raise PlatformError(400, "INVALID_REQUEST", "Idempotency-Key header is required")
        return platform.place_order(user, body.checkout_id, idempotency_key)

    @app.get("/orders")
    async def order_by_key(idempotency_key: str, authorization: str | None = Header(None)):
        return platform.order_view(platform.order_by_key(user_of(authorization), idempotency_key))

    @app.get("/orders/{order_id}")
    async def get_order(order_id: str, authorization: str | None = Header(None)):
        return platform.order_view(platform.get_order(user_of(authorization), order_id))

    @app.post("/orders/{order_id}/cancel")
    async def cancel_order(order_id: str, authorization: str | None = Header(None),
                           idempotency_key: str | None = Header(None)):
        user = user_of(authorization)
        if not idempotency_key:
            raise PlatformError(400, "INVALID_REQUEST", "Idempotency-Key header is required")
        return platform.cancel_order(user, order_id, idempotency_key)

    @app.get("/refunds/{ref}")
    async def refund(ref: str, authorization: str | None = Header(None)):
        user_of(authorization)
        return platform.get_refund(ref)

    # -- payment page (the user, in a browser) ----------------------------

    @app.get("/pay/{order_id}", response_class=HTMLResponse)
    async def pay_page(order_id: str):
        o = platform.orders.get(order_id)
        if not o:
            return _page("Not found", "<p>No such order.</p>", 404)
        if o["status"] != "pending_payment":
            return _page("Order " + order_id, f"<p>Status: {html.escape(o['status'])}</p>")
        form = (
            f"<p>Total: <b>{o['total']:,} VND</b> (payment status: {o['payment_status']})</p>"
            f"<form method=post><button name=outcome value=auto>Pay</button> "
            f"<button name=outcome value=fail>Decline</button></form>"
        )
        return _page("Pay order " + order_id, form)

    @app.post("/pay/{order_id}")
    async def pay(order_id: str, request: Request):
        form = parse_qs((await request.body()).decode())
        outcome = form.get("outcome", ["auto"])[0]
        if request.headers.get("accept", "").startswith("application/json"):
            return platform.pay(order_id, outcome)
        order = platform.pay(order_id, outcome)
        msg = "Payment received." if order["payment_status"] == "succeeded" else "Payment failed. You can try again."
        return _page("Order " + order_id, f"<p>{msg}</p><p><a href='/pay/{html.escape(order_id)}'>Back</a></p>")

    # -- OAuth login for the confirmation page ----------------------------

    def _check_client(client_id: str, redirect_uri: str) -> None:
        c = platform.clients.get(client_id)
        if not c or redirect_uri not in c["redirect_uris"]:
            raise PlatformError(400, "INVALID_CLIENT", "unknown client or redirect_uri")

    def _login_form(q: dict[str, str], error: str = "") -> HTMLResponse:
        hidden = "".join(
            f"<input type=hidden name={k} value='{html.escape(q.get(k, ''), quote=True)}'>"
            for k in ("client_id", "redirect_uri", "state")
        )
        err = f"<p style='color:#b00'>{html.escape(error)}</p>" if error else ""
        return _page("Sign in to the mock shop",
                     f"{err}<form method=post>{hidden}<p><input name=username placeholder=username></p>"
                     f"<p><input name=password type=password placeholder=password></p>"
                     f"<button>Sign in</button></form>", 401 if error else 200)

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    async def authorize_page(client_id: str, redirect_uri: str, state: str = ""):
        _check_client(client_id, redirect_uri)
        return _login_form({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})

    @app.post("/oauth/authorize")
    async def authorize(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        _check_client(form.get("client_id", ""), form.get("redirect_uri", ""))
        user = platform.login(form.get("username", ""), form.get("password", ""))
        if not user:
            return _login_form(form, "Wrong username or password")
        code = platform.issue_code(user, form["client_id"], form["redirect_uri"])
        sep = "&" if "?" in form["redirect_uri"] else "?"
        return RedirectResponse(form["redirect_uri"] + sep + urlencode({"code": code, "state": form.get("state", "")}),
                                status_code=302)

    @app.post("/oauth/token")
    async def token(request: Request):
        raw = (await request.body()).decode()
        if request.headers.get("content-type", "").startswith("application/json"):
            try:
                body = json.loads(raw or "{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                raise PlatformError(400, "INVALID_REQUEST", "body must be a JSON object")
            form = {k: str(v) for k, v in body.items()}
        else:
            form = {k: v[0] for k, v in parse_qs(raw).items()}
        return platform.exchange(form)

    @app.get("/me")
    async def me(authorization: str | None = Header(None)):
        user = user_of(authorization)
        return {"id": user, "name": platform.users[user]["name"]}

    @app.get("/me/addresses")
    async def addresses(authorization: str | None = Header(None)):
        return {"items": platform.users[user_of(authorization)]["addresses"]}

    # -- test control (not part of the shop's real API) --------------------

    @app.get("/_admin/config")
    async def get_config():
        return {k: v for k, v in vars(platform.config).items() if k != "webhook_secret"}

    @app.post("/_admin/config")
    async def set_config(body: ConfigIn):
        changes = body.model_dump(exclude_none=True)
        if "mode" in changes and changes["mode"] not in MODES:
            raise PlatformError(422, "INVALID_REQUEST", f"mode must be one of {sorted(MODES)}")
        for k, v in changes.items():
            setattr(platform.config, k, v)
        if "seed" in changes:
            platform.rng.seed(changes["seed"])
        return {k: v for k, v in vars(platform.config).items() if k != "webhook_secret"}

    @app.post("/_admin/reset")
    async def reset():
        platform.reset()
        return {"status": "reset"}

    @app.post("/_admin/clock")
    async def clock(advance_seconds: float):
        platform.advance(advance_seconds)
        return {"now": platform.iso(platform.now())}

    @app.post("/_admin/variants/{variant_id}")
    async def set_variant(variant_id: str, price: int | None = None, stock: int | None = None):
        v = platform.variants.get(variant_id)
        if not v:
            raise PlatformError(404, "NOT_FOUND", "variant not found")
        if price is not None:
            v.price = price
        if stock is not None:
            v.stock = stock
        return {"id": v.id, "price": v.price, "stock": v.stock, "reserved": v.reserved}

    @app.post("/_admin/orders/{order_id}/status")
    async def set_status(order_id: str, status: str):
        return platform.advance_order(order_id, status)

    @app.post("/_admin/refunds/{ref}/settle")
    async def settle(ref: str, ok: bool = True):
        return platform.settle_refund(ref, ok)

    @app.get("/_admin/events")
    async def events():
        return {"items": platform.events}

    @app.post("/_admin/clients")
    async def register_client(client_id: str, redirect_uri: str):
        """Test helper: add a redirect_uri for the seeded `ucp` client."""
        c = platform.clients.get(client_id)
        if not c:
            raise PlatformError(404, "NOT_FOUND", "unknown client")
        if redirect_uri not in c["redirect_uris"]:
            c["redirect_uris"].append(redirect_uri)
        return {"redirect_uris": c["redirect_uris"]}

    return app


app = create_app()
