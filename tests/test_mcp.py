"""Step 10: the MCP tools against a real UCP, and the OAuth sign-in for the HTTP transport."""
import asyncio
import base64
import hashlib
import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("mcp")

from mcp import Client  # noqa: E402

from mcp_server.__main__ import http_app  # noqa: E402
from mcp_server import server as mcp_tools  # noqa: E402
from mcp_server.auth import CLAUDE_CALLBACK, allowed_redirect  # noqa: E402
from mcp_server.server import build_server  # noqa: E402
from mcp_server.ucp_client import UCPClient  # noqa: E402
from tests.conftest import link  # noqa: E402
from tests.test_trade import FORM, agree_token, pay, press_agree, user_confirms, user_logs_in  # noqa: E402
from ucp import outbox  # noqa: E402
from ucp.common.config import get_settings  # noqa: E402

INTENTS = {"create_checkout", "select_shipping_method", "apply_promotion", "extend_checkout", "cancel_checkout",
           "confirm_order", "cancel_order"}
READS = {"account_status", "link_account", "search_products", "get_product", "shipping_options", "get_order",
         "get_events"}
PASSWORD = "correct horse battery"


class Agent:
    """Calls tools the way Claude does, in process, and keeps everything it was told."""

    def __init__(self, ucp_http):
        made = ucp_http.post("/v1/agents", json={"name": "claude"}).json()
        self.id, self.key = made["agent_id"], made["api_key"]
        self.client = UCPClient(ucp_http, self.key)
        self.server = build_server(self.client)
        self.seen: list[str] = []

    def tool(self, name, **args) -> tuple[bool, str]:
        async def run():
            async with Client(self.server) as c:
                return await c.call_tool(name, args)

        r = asyncio.run(run())
        text = "".join(getattr(b, "text", "") for b in r.content)
        self.seen.append(text)
        return not r.is_error, text

    def ok(self, name, **args) -> dict:
        good, text = self.tool(name, **args)
        assert good, text
        return json.loads(text)

    def error(self, name, **args) -> tuple[dict, str]:
        good, text = self.tool(name, **args)
        assert not good, text
        return json.JSONDecoder().raw_decode(text[text.index("{"):])[0], text


@pytest.fixture
def claude(ucp):
    a = Agent(ucp)
    token = ucp.post("/v1/auth/token", json={"grant_type": "api_key"}, headers={"X-API-Key": a.key}).json()["token"]
    link(ucp, a.id, {"Authorization": f"Bearer {token}"})
    return a


def test_one_tool_per_intent_plus_the_reads(claude):
    async def run():
        async with Client(claude.server) as c:
            return (await c.list_tools()).tools

    assert {t.name for t in asyncio.run(run())} == INTENTS | READS


def test_a_whole_purchase_and_cancel_through_the_tools(ucp, claude):
    assert "create_order" in claude.ok("account_status")["permissions"]
    assert claude.ok("search_products", query="mug")["products"][0]["product_id"] == "p_mug"
    assert claude.ok("get_product", product_id="p_mug")["variants"]
    assert [o["method_id"] for o in claude.ok("shipping_options")["options"]] == ["standard", "express"]
    co = claude.ok("create_checkout", items=[{"product_id": "p_mug", "qty": 2}])
    cid = co["checkout_id"]
    assert claude.ok("select_shipping_method", checkout_id=cid, shipping_method_id="express")["proposal"]
    assert claude.ok("apply_promotion", checkout_id=cid, code="SAVE20K")["proposal"]["discount"] == 20_000
    assert claude.ok("extend_checkout", checkout_id=cid)["confirm_url"] == co["confirm_url"]
    err, _ = claude.error("confirm_order", checkout_id=cid)  # the agent cannot confirm by itself
    assert err["code"] == "UCP_CONFIRMATION_REQUIRED" and not ucp.shop.app.state.platform.orders
    user_confirms(ucp, cid)
    order = claude.ok("confirm_order", checkout_id=cid)
    pay(ucp, order["payment_url"])
    outbox.relay()
    assert "OrderPlaced" in [e["event_type"] for e in claude.ok("get_events")["events"]]
    assert claude.ok("get_order", order_id=order["order_id"])["status"] == "placed"

    err, _ = claude.error("cancel_order", order_id=order["order_id"], reason="changed my mind")
    request_id = err["details"]["confirm_url"].rsplit("/", 1)[1]  # the link has to reach the model
    page = user_logs_in(ucp, request_id)
    token = re.search(r"name=token value='([^']+)'", page.text).group(1)
    ucp.post(f"/v1/confirm/{request_id}", content=f"token={token}", headers=FORM)
    done = claude.ok("cancel_order", order_id=order["order_id"])
    assert done["status"] == "cancelled" and "/order/" in done["order_url"]
    assert done["order_url"] in done["show_the_user"]

    spare = claude.ok("create_checkout", items=[{"product_id": "p_mug", "qty": 1}])
    assert claude.ok("cancel_checkout", checkout_id=spare["checkout_id"])["status"] == "cancelled"
    # nothing the model was told holds the API key or a session token (rule 13)
    told = "\n".join(claude.seen)
    assert claude.key not in told and claude.client._token not in told


def test_link_account_gives_the_user_a_url(ucp):
    a = Agent(ucp)
    err, _ = a.error("create_checkout", items=[{"product_id": "p_mug", "qty": 1}])
    assert err["code"] == "UCP_NO_USER_LINK"
    assert a.ok("link_account")["auth_url"].startswith("http://localhost:9000/oauth/authorize")


def test_retrying_with_the_named_key_replays_instead_of_buying_twice(ucp, claude):
    items = [{"product_id": "p_mug", "qty": 1}]
    first = claude.ok("create_checkout", items=items, idempotency_key="k-1")
    assert claude.ok("create_checkout", items=items, idempotency_key="k-1") == first
    _, text = claude.error("confirm_order", checkout_id=first["checkout_id"])
    assert "idempotency_key=" in text


def test_expired_session_is_renewed(claude):
    claude.ok("search_products")
    old = jwt.encode({"sub": claude.id, "exp": datetime.now(UTC) - timedelta(seconds=5)},
                     get_settings().jwt_secret, "HS256")
    claude.client._token = old
    claude.ok("search_products")
    assert claude.client._token != old


def test_bad_api_key_and_a_dead_api_say_what_happened():
    def down(request):
        raise httpx.ConnectError("no")

    a = build_server(UCPClient(httpx.Client(transport=httpx.MockTransport(down), base_url="http://x"), "ucp_secret"))

    async def run():
        async with Client(a) as c:
            return await c.call_tool("search_products", {})

    r = asyncio.run(run())
    assert r.is_error and "did not answer" in r.content[0].text and "ucp_secret" not in r.content[0].text


# -- waiting for the user, so they never have to report back ----------------------

@pytest.fixture
def quick_waits(monkeypatch):
    monkeypatch.setattr(mcp_tools, "WAIT_SECONDS", 5)
    monkeypatch.setattr(mcp_tools, "POLL_SECONDS", 0.05)


def acts_during_the_wait(monkeypatch, user_acts):
    """Runs the user's click the first time a tool pauses, while its call is still open."""
    done = []

    def pause(_):
        if not done:
            done.append(True)
            user_acts()

    monkeypatch.setattr(mcp_tools.time, "sleep", pause)
    return done


def test_one_waiting_call_finishes_once_the_user_agrees_and_pays(ucp, claude, monkeypatch, quick_waits):
    co = claude.ok("create_checkout", items=[{"product_id": "p_mug", "qty": 1}])
    def agree(proposal_id):  # the real button: UCP does the action itself
        press_agree(ucp, proposal_id, agree_token(ucp, proposal_id)[1])

    clicked = acts_during_the_wait(monkeypatch, lambda: agree(co["checkout_id"]))
    order = claude.ok("confirm_order", checkout_id=co["checkout_id"], wait_for_user=True)
    assert clicked and order["payment_url"] and len(ucp.shop.app.state.platform.orders) == 1
    paid = acts_during_the_wait(monkeypatch, lambda: pay(ucp, order["payment_url"]))
    assert claude.ok("get_order", order_id=order["order_id"], wait_for_payment=True)["status"] == "placed" and paid

    err, _ = claude.error("cancel_order", order_id=order["order_id"])  # first call hands over the link at once
    request_id = err["details"]["confirm_url"].rsplit("/", 1)[1]

    agreed = acts_during_the_wait(monkeypatch, lambda: agree(request_id))
    assert claude.ok("cancel_order", order_id=order["order_id"], wait_for_user=True)["status"] == "cancelled" and agreed


def test_waiting_cannot_stand_in_for_the_user(ucp, claude, monkeypatch, quick_waits):
    monkeypatch.setattr(mcp_tools, "WAIT_SECONDS", 0.3)
    co = claude.ok("create_checkout", items=[{"product_id": "p_mug", "qty": 1}])
    err, text = claude.error("confirm_order", checkout_id=co["checkout_id"], wait_for_user=True)
    assert err["code"] == "UCP_CONFIRMATION_REQUIRED" and "has not agreed yet" in text
    assert not ucp.shop.app.state.platform.orders


# -- OAuth for the HTTP transport ----------------------------------------------

PUBLIC = "https://demo.example.com"


@pytest.fixture
def web():
    ucp = UCPClient(httpx.Client(base_url="http://127.0.0.1:1"), "ucp_unused")
    with TestClient(http_app(ucp, PUBLIC, PASSWORD, 8765), base_url=PUBLIC) as c:
        yield c


def register(web, redirect=CLAUDE_CALLBACK):
    return web.post("/register", json={"redirect_uris": [redirect], "token_endpoint_auth_method": "none",
                                       "grant_types": ["authorization_code", "refresh_token"],
                                       "response_types": ["code"], "client_name": "Claude"})


def start(web, client_id, **extra):
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    r = web.get("/authorize", params={"client_id": client_id, "redirect_uri": CLAUDE_CALLBACK, "state": "s1",
                                      "response_type": "code", "code_challenge": challenge,
                                      "code_challenge_method": "S256", "resource": f"{PUBLIC}/mcp", **extra},
                follow_redirects=False)
    return r, verifier


def sign_in(web, location, password=PASSWORD):
    txn = parse_qs(urlparse(location).query)["txn"][0]
    return web.post("/login", data={"txn": txn, "password": password}, follow_redirects=False)


def exchange(web, client_id, code, verifier):
    return web.post("/token", data={"grant_type": "authorization_code", "client_id": client_id, "code": code,
                                    "code_verifier": verifier, "redirect_uri": CLAUDE_CALLBACK})


MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def mcp_call(web, token=None):
    auth = {"authorization": f"Bearer {token}"} if token else {}
    return web.post("/mcp", json=INIT, headers={**MCP_HEADERS, **auth})


def test_the_whole_sign_in_then_rotation(web):
    r = mcp_call(web)
    assert r.status_code == 401 and "oauth-protected-resource" in r.headers["www-authenticate"]
    assert mcp_call(web, "made-up").status_code == 401
    meta = web.get("/.well-known/oauth-protected-resource/mcp").json()
    assert meta["resource"] == f"{PUBLIC}/mcp"
    server = web.get("/.well-known/oauth-authorization-server").json()
    assert "S256" in server["code_challenge_methods_supported"] and server["registration_endpoint"]

    client_id = register(web).json()["client_id"]
    started, verifier = start(web, client_id)
    page = web.get(started.headers["location"])
    assert "claude.ai" in page.text and page.headers["x-frame-options"] == "DENY"
    done = sign_in(web, started.headers["location"])
    back = parse_qs(urlparse(done.headers["location"]).query)
    assert done.headers["location"].startswith(CLAUDE_CALLBACK) and back["state"] == ["s1"]
    assert exchange(web, client_id, back["code"][0], "wrong-verifier" * 4).status_code == 400  # PKCE
    tokens = exchange(web, client_id, back["code"][0], verifier).json()
    assert exchange(web, client_id, back["code"][0], verifier).status_code == 400  # single use
    assert mcp_call(web, tokens["access_token"]).status_code == 200
    refresh = {"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]}
    assert web.post("/token", data=refresh).status_code == 200
    again = web.post("/token", data=refresh)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_wrong_password_gives_nothing_and_then_locks(web):
    started, _ = start(web, register(web).json()["client_id"])
    for _ in range(5):
        r = sign_in(web, started.headers["location"], "guess")
        assert r.status_code == 401 and "location" not in r.headers
    assert sign_in(web, started.headers["location"]).status_code == 429  # even the right one waits


def test_a_strangers_redirect_address_cannot_register(web):
    for bad in ("https://evil.example/cb", "https://claude.ai.evil.example/api/mcp/auth_callback",
                "http://localhost.evil.example/callback", "http://localhost@evil.example/callback"):
        assert register(web, bad).status_code == 400, bad
        assert not allowed_redirect(bad)
    assert allowed_redirect("http://127.0.0.1:51789/callback") and allowed_redirect("http://localhost/callback")


def test_unknown_host_and_other_resources_are_refused(web):
    client_id = register(web).json()["client_id"]
    r, _ = start(web, client_id, resource="https://other.example/mcp")
    assert "invalid_target" in r.headers.get("location", r.text)
    started, verifier = start(web, client_id)
    code = parse_qs(urlparse(sign_in(web, started.headers["location"]).headers["location"]).query)["code"][0]
    token = exchange(web, client_id, code, verifier).json()["access_token"]
    r = web.post("/mcp", json=INIT, headers={**MCP_HEADERS, "authorization": f"Bearer {token}",
                                             "host": "evil.example"})
    assert r.status_code in (400, 403, 421)


@pytest.mark.parametrize("password", ["", "short"])
def test_http_refuses_to_start_without_a_real_password(password):
    with pytest.raises(SystemExit):
        http_app(UCPClient(httpx.Client(), "k"), PUBLIC, password, 8765)
