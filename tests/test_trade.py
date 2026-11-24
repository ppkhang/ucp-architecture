"""Step 6: checkout, the user's confirmation, confirm_order, webhooks. The first end-to-end slice."""
import re
from urllib.parse import parse_qs, urlparse

import httpx

from tests.conftest import agent, link
from ucp.api.main import app
from ucp.common.db import connect

MUG = [{"product_id": "p_mug", "qty": 2}]  # 2 x 90,000 + HCM shipping 20,000 = 200,000
FORM = {"content-type": "application/x-www-form-urlencoded"}


def intent(c, auth, key, method, params):
    return c.post("/v1/intent", json={"method": method, "params": params}, headers={**auth, "Idempotency-Key": key})


def checkout(c, auth, key="co-1", items=MUG):
    r = intent(c, auth, key, "create_checkout", {"platform_id": "mock", "items": items})
    assert r.status_code == 200, r.text
    return r.json()


def user_logs_in(c, checkout_id, user="khang", password="khang123"):
    """The user opens confirm_url, logs in at the shop, lands back on UCP. Returns the callback response."""
    r = c.get(f"/v1/confirm/{checkout_id}", follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    login = c.shop.post("/oauth/authorize", data={"client_id": q["client_id"][0], "redirect_uri": q["redirect_uri"][0],
                                                  "state": q["state"][0], "username": user, "password": password},
                        follow_redirects=False)
    back = parse_qs(urlparse(login.headers["location"]).query)
    return c.get("/oauth/callback", params={"code": back["code"][0], "state": back["state"][0]})


def user_confirms(c, checkout_id):
    page = user_logs_in(c, checkout_id)
    assert page.status_code == 200
    token = re.search(r"name=token value='([^']+)'", page.text).group(1)
    r = c.post(f"/v1/confirm/{checkout_id}", content=f"token={token}", headers=FORM)
    assert r.status_code == 200
    return page


def confirm_order(c, auth, checkout_id, key="ord-1"):
    return intent(c, auth, key, "confirm_order", {"checkout_id": checkout_id})


def setup(c, user="khang", password="khang123"):
    agent_id, _, auth = agent(c)
    link(c, agent_id, auth, user, password)
    return agent_id, auth


def pay(c, payment_url):
    path = urlparse(payment_url).path
    r = c.shop.post(path, data={"outcome": "auto"}, headers={"accept": "application/json"})
    assert r.status_code == 200
    return r.json()


def code(r):
    return r.json()["error"]["code"]


def test_full_purchase(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    assert co["proposal"]["total"] == 200_000 and co["proposal"]["stock_guaranteed"] is True
    assert co["proposal"]["items"][0]["name"] == "Ceramic mug (default)"
    assert co["hold_expires_at"] and co["expires_at"] <= co["hold_expires_at"]
    page = user_confirms(ucp, co["checkout_id"])
    assert "Ceramic mug" in page.text and "200,000" in page.text  # UCP's snapshot, shown to the user
    r = confirm_order(ucp, auth, co["checkout_id"])
    assert r.status_code == 200
    order = r.json()
    assert order["status"] == "pending_payment" and order["payment_url"].startswith("http://localhost:9000/pay/")
    got = ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()
    assert (got["status"], got["reconciling"], got["total"]) == ("pending_payment", False, 200_000)
    pay(ucp, order["payment_url"])  # the shop sends a signed webhook
    got = ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()
    assert got["status"] == "placed" and len(got["platform_order_ids"]) == 1


def test_agent_cannot_confirm_by_itself(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    r = confirm_order(ucp, auth, co["checkout_id"])
    assert (r.status_code, code(r)) == (403, "UCP_CONFIRMATION_REQUIRED")
    assert not ucp.shop.app.state.platform.orders


def test_wrong_shop_user_cannot_confirm(ucp):
    _, auth = setup(ucp)  # linked to khang
    co = checkout(ucp, auth)
    r = user_logs_in(ucp, co["checkout_id"], "linh", "linh123")
    assert (r.status_code, code(r)) == (403, "UCP_FORBIDDEN")
    assert confirm_order(ucp, auth, co["checkout_id"]).status_code == 403


def test_confirmation_is_single_use(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    first = confirm_order(ucp, auth, co["checkout_id"], "ord-1").json()
    again = confirm_order(ucp, auth, co["checkout_id"], "ord-2").json()  # new key, same checkout
    assert again["order_id"] == first["order_id"] and len(ucp.shop.app.state.platform.orders) == 1


def test_stale_confirmation_fails_loudly_and_resubmit_is_harmless(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    token = re.search(r"name=token value='([^']+)'", user_logs_in(ucp, co["checkout_id"]).text).group(1)
    with connect() as conn:  # the proposal changed after the user logged in
        conn.execute("UPDATE trade.checkouts SET proposal_hash = 'other' WHERE id = %s", (co["checkout_id"],))
    r = ucp.post(f"/v1/confirm/{co['checkout_id']}", content=f"token={token}", headers=FORM)
    assert (r.status_code, code(r)) == (403, "UCP_FORBIDDEN")  # not a silent "Confirmed"
    assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_CONFIRMATION_REQUIRED"
    co = checkout(ucp, auth, "co-2")
    token = re.search(r"name=token value='([^']+)'", user_logs_in(ucp, co["checkout_id"]).text).group(1)
    for _ in range(2):  # a double submit of the same confirmation still shows "Confirmed"
        assert ucp.post(f"/v1/confirm/{co['checkout_id']}", content=f"token={token}", headers=FORM).status_code == 200


def test_other_agent_cannot_use_a_checkout(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    _, other = setup(ucp, "linh", "linh123")
    assert code(confirm_order(ucp, other, co["checkout_id"])) == "UCP_NOT_FOUND"


def test_idempotency_replay_conflict_and_missing_key(ucp):
    _, auth = setup(ucp)
    a, b = checkout(ucp, auth, "same"), checkout(ucp, auth, "same")
    assert a == b and len(ucp.shop.app.state.platform.checkouts) == 1
    r = intent(ucp, auth, "same", "create_checkout", {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 3}]})
    assert (r.status_code, code(r)) == (409, "UCP_IDEMPOTENCY_CONFLICT")
    r = ucp.post("/v1/intent", json={"method": "create_checkout", "params": {}}, headers=auth)
    assert (r.status_code, code(r)) == (400, "UCP_BAD_REQUEST")
    with connect() as conn:  # the 24 hour window: an old key is forgotten
        conn.execute("UPDATE trade.idempotency SET created_at = now() - interval '25 hours'")
    assert checkout(ucp, auth, "same")["checkout_id"] != a["checkout_id"]


def test_timeout_then_retry_gives_one_order(ucp):
    agent_id, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    shop, seen = ucp.shop, {"timeouts": 2}

    def flaky(request):  # the shop gets the request, but the answer is lost, twice (placeOrder, then the lookup)
        r = shop.request(request.method, str(request.url), content=request.content, headers=request.headers)
        if request.url.path == "/orders" and seen["timeouts"]:
            seen["timeouts"] -= 1
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    app.state.platform_http = httpx.Client(transport=httpx.MockTransport(flaky))
    r = confirm_order(ucp, auth, co["checkout_id"], "retry-me")
    assert (r.status_code, code(r)) == (503, "UCP_PLATFORM_UNAVAILABLE")
    with connect() as conn:
        row = conn.execute("SELECT reconciling, platform_order_id FROM trade.orders").fetchone()
    assert (row["reconciling"], row["platform_order_id"]) == (True, None)  # marked, not resent
    r = confirm_order(ucp, auth, co["checkout_id"], "retry-me")
    assert r.status_code == 200
    platform = shop.app.state.platform
    assert len(platform.orders) == 1 and f"{agent_id}:retry-me" in platform.order_keys  # one charge, namespaced key
    assert ucp.get(f"/v1/orders/{r.json()['order_id']}", headers=auth).json()["reconciling"] is False


def test_unlink_mid_session_refuses_confirm(ucp):
    agent_id, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    with connect() as conn:
        link_id = conn.execute("SELECT id FROM identity.user_links").fetchone()["id"]
    assert ucp.delete(f"/v1/agents/{agent_id}/links/{link_id}", headers=auth).status_code == 204
    r = confirm_order(ucp, auth, co["checkout_id"])
    assert (r.status_code, code(r)) == (403, "UCP_NO_USER_LINK")


def test_no_link_no_checkout(ucp):
    _, _, auth = agent(ucp)
    r = intent(ucp, auth, "k", "create_checkout", {"platform_id": "mock", "items": MUG})
    assert (r.status_code, code(r)) == (403, "UCP_NO_USER_LINK")


def test_last_unit_goes_to_one_agent(ucp):
    _, a = setup(ucp)
    _, b = setup(ucp, "linh", "linh123")
    lamp = [{"product_id": "p_lamp", "qty": 1}]
    checkout(ucp, a, items=lamp)  # the shop holds the unit
    r = intent(ucp, b, "k", "create_checkout", {"platform_id": "mock", "items": lamp})
    assert (r.status_code, code(r)) == (409, "UCP_INSUFFICIENT_STOCK")


def test_price_up_needs_the_user_again_price_down_applies(ucp):
    _, auth = setup(ucp)
    up, down = checkout(ucp, auth, "k1"), checkout(ucp, auth, "k2")
    user_confirms(ucp, up["checkout_id"])
    user_confirms(ucp, down["checkout_id"])
    ucp.shop.post("/_admin/variants/v_mug", params={"price": 100_000})
    r = confirm_order(ucp, auth, up["checkout_id"], "o1")
    assert (r.status_code, code(r)) == (409, "UCP_PRICE_CHANGED")
    assert not ucp.shop.app.state.platform.orders
    ucp.shop.post("/_admin/variants/v_mug", params={"price": 80_000})
    r = confirm_order(ucp, auth, down["checkout_id"], "o2")
    assert r.status_code == 200
    assert ucp.get(f"/v1/orders/{r.json()['order_id']}", headers=auth).json()["total"] == 180_000


def test_expired_checkout(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET expires_at = now() - interval '1 second'")
    assert (confirm_order(ucp, auth, co["checkout_id"]).status_code, ) == (410,)
    assert ucp.get(f"/v1/confirm/{co['checkout_id']}", follow_redirects=False).status_code == 410


def test_webhook_signature_duplicates_and_unknown_order(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    order = confirm_order(ucp, auth, co["checkout_id"]).json()
    pay(ucp, order["payment_url"])
    headers, body = ucp.deliveries[-1]
    url = "/v1/platform-events/mock"
    assert ucp.post(url, content=body, headers={**headers, "X-Mock-Signature": "sha256=" + "0" * 64}).status_code == 401
    assert ucp.post(url, content=body + b" ", headers=headers).status_code == 401  # body changed after signing
    assert ucp.post(url, content=body, headers=headers).status_code == 202  # duplicate: accepted, applied once
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM trade.platform_events").fetchone()["n"] == 1
    assert ucp.post("/v1/platform-events/shopee", content=body, headers=headers).status_code == 404
    ucp.shop.app.state.platform.emit("order.paid", {"order_id": "ord_unknown"})  # no such order: ignored
    assert ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()["status"] == "placed"


def test_stale_order_event_cannot_move_status_backwards(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    order = confirm_order(ucp, auth, co["checkout_id"]).json()
    pay(ucp, order["payment_url"])
    plat_id = ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()["platform_order_ids"][0]
    ucp.shop.app.state.platform.emit("order.status_changed", {"order_id": plat_id, "status": "pending_payment"})
    assert ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()["status"] == "placed"


def test_orders_are_private_to_the_agent(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    order = confirm_order(ucp, auth, co["checkout_id"]).json()
    _, other = setup(ucp, "linh", "linh123")
    assert ucp.get(f"/v1/orders/{order['order_id']}", headers=other).status_code == 404
