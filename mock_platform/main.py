"""The fake shop: a separate FastAPI app that UCP's MockPlatformAdapter talks to."""

from __future__ import annotations

import asyncio
import html
import json
import os
import secrets
from pathlib import Path
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Header, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mock_platform.store import MODES, SESSION_SECONDS, Config, Platform, PlatformError

STATIC = Path(__file__).parent / "static"
LINK_TAIL = ")]}>.,;:!?'\"`* "  # punctuation a chat can glue to a link
# Cookies ignore ports, so on localhost the UCP API receives this one too. A real shop has its own domain.
COOKIE = "mock_shop_session"


class Item(BaseModel):
    variant_id: str
    quantity: int = Field(ge=1, le=100)


class QuoteIn(BaseModel):
    items: list[Item]
    region: str = "HCM"
    shipping_method_id: str | None = None
    promo_code: str | None = None


class CheckoutIn(QuoteIn):
    address_id: str | None = None


class OrderIn(BaseModel):
    checkout_id: str


class LoginIn(BaseModel):
    username: str
    password: str


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
        f"<!doctype html><html lang=en><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width, initial-scale=1'><title>{html.escape(title)}</title>"
        f"<link rel=stylesheet href=/static/shop.css>"
        f"<header class=mast><h1>Mock shop</h1></header><main class=narrow>{body}</main>",
        status_code=status,
        headers={"X-Frame-Options": "DENY", "Cache-Control": "no-store"},  # the buttons must not be framed
    )


def _waybill(heading: str, body: str) -> str:
    return (f"<article class=waybill><div class=wb-head><h2 class=route>{html.escape(heading)}</h2></div>"
            f"{body}</article>")


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

    # -- the shop's own pages ---------------------------------------------

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(STATIC / "index.html")

    def remember(response, user: str) -> None:
        # Lax, not Strict: the browser arrives here by following UCP's redirect, and must still be signed in
        response.set_cookie(COOKIE, platform.start_session(user), max_age=SESSION_SECONDS, httponly=True,
                            samesite="lax")

    def forget(request: Request, response) -> None:
        platform.end_session(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)

    @app.get("/shop/session")
    async def shop_session(request: Request):
        """For the "My orders" page: who is signed in, and their orders."""
        s = platform.session(request.cookies.get(COOKIE))
        user = s["user"] if s else None
        mine = [o for o in reversed(list(platform.orders.values())) if o["user"] == user]
        alerts = [a for a in platform.alerts if a["user"] == user and not a["seen"]]
        return {"user": user, "items": [platform.ui_order(o) for o in mine], "alerts": alerts}

    @app.delete("/shop/alerts", status_code=204)  # DELETE, so another site cannot send it without a preflight
    async def dismiss_alerts(request: Request):
        s = platform.session(request.cookies.get(COOKIE))
        for a in platform.alerts:
            if s and a["user"] == s["user"]:
                a["seen"] = True

    @app.post("/shop/session", status_code=204)
    async def sign_in(body: LoginIn, request: Request, response: Response):
        user = platform.login(body.username, body.password)
        if not user:
            raise PlatformError(401, "UNAUTHORIZED", "Wrong username or password")
        platform.end_session(request.cookies.get(COOKIE))
        remember(response, user)

    @app.delete("/shop/session", status_code=204)
    async def sign_out(request: Request, response: Response):
        forget(request, response)

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
        return platform.quote([i.model_dump() for i in body.items], body.region, body.shipping_method_id,
                              body.promo_code)

    @app.get("/shipping/options")
    async def shipping_options(region: str = "HCM"):
        return {"items": platform.shipping_options(region)}

    # -- checkout and orders (user token) ---------------------------------

    @app.post("/checkouts", status_code=201)
    async def create_checkout(body: CheckoutIn, authorization: str | None = Header(None)):
        user = user_of(authorization)
        return platform.create_checkout(user, [i.model_dump() for i in body.items], body.region, body.address_id,
                                        body.shipping_method_id, body.promo_code)

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
                           idempotency_key: str | None = Header(None), only_if_unpaid: bool = False):
        user = user_of(authorization)
        if not idempotency_key:
            raise PlatformError(400, "INVALID_REQUEST", "Idempotency-Key header is required")
        return platform.cancel_order(user, order_id, idempotency_key, only_if_unpaid)

    @app.get("/refunds/{ref}")
    async def refund(ref: str, authorization: str | None = Header(None)):
        user_of(authorization)
        return platform.get_refund(ref)

    # -- payment page (the user, in a browser) ----------------------------

    def _pay_body(o: dict) -> str:
        rows = "".join(f"<li><span>{ln['quantity']} x {html.escape(ln['product_name'])} ({html.escape(ln['name'])})</span>"
                       f"<span>{ln['amount']:,} VND</span></li>" for ln in platform.ui_order(o)["lines"])
        extra = f"<li><span>Shipping</span><span>{o['shipping_fee']:,} VND</span></li>"
        if o["discount"]:
            extra += f"<li><span>Discount</span><span>-{o['discount']:,} VND</span></li>"
        return (f"<ul class=items>{rows}{extra}<li><strong>Total</strong><strong>{o['total']:,} VND</strong></li></ul>")

    @app.get("/pay/{order_id}", response_class=HTMLResponse)
    async def pay_page(order_id: str):
        order_id = order_id.rstrip(LINK_TAIL)
        o = platform.orders.get(order_id)
        if not o:
            return _page("Not found", _waybill("Not found", "<p class=items>No such order.</p>"), 404)
        if o["status"] != "pending_payment":
            status = html.escape(o["status"].replace("_", " "))
            return _page("Order " + order_id, _waybill(
                "Nothing to pay", f"{_pay_body(o)}<p class=items>This order is {status}.</p>"))
        failed = "<p class=error>The last payment failed. You can try again.</p>" if o["payment_status"] == "failed" else ""
        form = (f"<form method=post>{failed}<button class=primary name=outcome value=auto>Pay {o['total']:,} VND</button>"
                f"<button name=outcome value=fail>Decline payment</button></form>"
                f"<p class='items muted'>This is a test shop. No money moves.</p>")
        return _page("Pay order " + order_id, _waybill("Pay your order", _pay_body(o) + form))

    @app.post("/pay/{order_id}")
    async def pay(order_id: str, request: Request):
        order_id = order_id.rstrip(LINK_TAIL)
        form = parse_qs((await request.body()).decode())
        outcome = form.get("outcome", ["auto"])[0]
        if request.headers.get("accept", "").startswith("application/json"):
            return platform.pay(order_id, outcome)
        order = platform.pay(order_id, outcome)
        paid = order["payment_status"] == "succeeded"
        msg = "Payment received." if paid else "Payment failed. You can try again."
        back = "" if paid else f"<a class=button href='/pay/{html.escape(order_id)}'>Try again</a>"
        return _page("Order " + order_id, _waybill(
            msg, f"<div class=actions>{back}<a class=button href='/#orders'>See my orders</a></div>"))

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
        err = f"<p class=error role=alert>{html.escape(error)}</p>" if error else ""
        return _page("Sign in to the mock shop", _waybill(
            "Sign in to continue",
            f"<form method=post>{err}{hidden}"
            f"<label>Username <input name=username autocomplete=username required></label>"
            f"<label>Password <input name=password type=password autocomplete=current-password required></label>"
            f"<button class=primary>Sign in</button></form>"
            f"<p class='items muted'>Your password stays with the shop. UCP never sees it.</p>"),
            401 if error else 200)

    def _continue_form(q: dict[str, str], s: dict, error: str = "") -> HTMLResponse:
        hidden = "".join(
            f"<input type=hidden name={k} value='{html.escape(q.get(k, ''), quote=True)}'>"
            for k in ("client_id", "redirect_uri", "state")
        )
        who = html.escape(s["user"])
        return _page("Continue to UCP", _waybill(
            "Signed in as " + s["user"],
            f"<form method=post>{hidden}<input type=hidden name=csrf value='{s['csrf']}'>"
            f"<p>UCP is asking to use your shop account. Continue only if you started this from your assistant.</p>"
            f"<button class=primary name=action value=continue>Continue as {who}</button>"
            f"<button name=action value=switch>Use another account</button></form>"
            f"<p class='items muted'>Continuing does not buy or cancel anything. You agree to that on the next page.</p>"))

    def _back_to_client(form: dict[str, str], user: str) -> RedirectResponse:
        code = platform.issue_code(user, form["client_id"], form["redirect_uri"])
        sep = "&" if "?" in form["redirect_uri"] else "?"
        return RedirectResponse(form["redirect_uri"] + sep + urlencode({"code": code, "state": form.get("state", "")}),
                                status_code=302)

    @app.get("/oauth/authorize", response_class=HTMLResponse)
    async def authorize_page(request: Request, client_id: str, redirect_uri: str, state: str = ""):
        _check_client(client_id, redirect_uri)
        q = {"client_id": client_id, "redirect_uri": redirect_uri, "state": state}
        s = platform.session(request.cookies.get(COOKIE))
        if s and platform.clients[client_id].get("sign_in_only"):
            return _back_to_client(q, s["user"])
        return _continue_form(q, s) if s else _login_form(q)

    @app.post("/oauth/authorize")
    async def authorize(request: Request):
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        _check_client(form.get("client_id", ""), form.get("redirect_uri", ""))
        action = form.get("action")
        if action == "switch":
            page = _login_form(form)
            forget(request, page)
            return page
        if action == "continue":
            s = platform.session(request.cookies.get(COOKIE))
            # the token is only in the page the shop served, so another page cannot press Continue for the user
            if not s or not secrets.compare_digest(form.get("csrf", "").encode(), s["csrf"].encode()):
                return _login_form(form, "Please sign in again")
            return _back_to_client(form, s["user"])
        user = platform.login(form.get("username", ""), form.get("password", ""))
        if not user:
            return _login_form(form, "Wrong username or password")
        platform.end_session(request.cookies.get(COOKIE))
        done = _back_to_client(form, user)
        remember(done, user)
        return done

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

    @app.get("/_admin/state")
    async def admin_state():
        platform.tick()  # admin paths skip the middleware's tick, and the panel should show lapsed holds
        return platform.snapshot()

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
        if client_id == "ucp" and redirect_uri not in platform.clients["ucp-confirm"]["redirect_uris"]:
            platform.clients["ucp-confirm"]["redirect_uris"].append(redirect_uri)
        if redirect_uri not in c["redirect_uris"]:
            c["redirect_uris"].append(redirect_uri)
        return {"redirect_uris": c["redirect_uris"]}

    return app


app = create_app()
