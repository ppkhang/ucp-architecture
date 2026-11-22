import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mock_platform.main import create_app
from mock_platform.store import Config, sign

REDIRECT = "http://localhost:8000/oauth/callback"
CLIENT = {"client_id": "ucp", "client_secret": "ucp-mock-secret"}


@pytest.fixture
def shop():
    app = create_app(Config(slow_delay=0.05, webhook_secret="s3cret"))
    c = TestClient(app)
    c.platform = app.state.platform
    c.sent = []
    c.platform.sender = lambda event, headers, body: c.sent.append((event, headers, body))
    return c


def login(shop, user="khang", password="khang123") -> dict:
    r = shop.post("/oauth/authorize", data={"client_id": "ucp", "redirect_uri": REDIRECT, "state": "xyz",
                                            "username": user, "password": password}, follow_redirects=False)
    assert r.status_code == 302
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    t = shop.post("/oauth/token", data={**CLIENT, "grant_type": "authorization_code",
                                        "code": q["code"][0], "redirect_uri": REDIRECT})
    assert t.status_code == 200
    return t.json()


def auth(tokens) -> dict:
    return {"Authorization": "Bearer " + tokens["access_token"]}


def checkout(shop, tokens, variant="v_mug", qty=1, **kw):
    return shop.post("/checkouts", json={"items": [{"variant_id": variant, "quantity": qty}], **kw},
                     headers=auth(tokens))


def place(shop, tokens, checkout_id, key="k1"):
    return shop.post("/orders", json={"checkout_id": checkout_id},
                     headers={**auth(tokens), "Idempotency-Key": key})


def order_flow(shop, variant="v_mug", qty=1):
    tokens = login(shop)
    co = checkout(shop, tokens, variant, qty).json()
    order = place(shop, tokens, co["id"]).json()
    return tokens, co, order


# -- catalog -------------------------------------------------------------


def test_search_and_detail(shop):
    items = shop.get("/products", params={"q": "mug"}).json()["items"]
    assert [p["id"] for p in items] == ["p_mug"]
    p = shop.get("/products/p_tee").json()
    assert {v["id"]: v["in_stock"] for v in p["variants"]}["v_tee_m_white"] is False
    assert shop.get("/products/nope").status_code == 404


def test_quote_prices_in_integer_vnd(shop):
    q = shop.post("/quote", json={"items": [{"variant_id": "v_mug", "quantity": 2}], "region": "HN"}).json()
    assert q["subtotal"] == 180_000 and q["shipping_fee"] == 25_000 and q["total"] == 205_000
    assert q["in_stock"] is True


def test_capabilities(shop):
    caps = shop.get("/capabilities").json()
    assert caps["holds"] is True and caps["webhooks"] is True and caps["cod"] is False


# -- oauth ---------------------------------------------------------------


def test_wrong_password_is_refused(shop):
    r = shop.post("/oauth/authorize", data={"client_id": "ucp", "redirect_uri": REDIRECT,
                                            "username": "khang", "password": "bad"}, follow_redirects=False)
    assert r.status_code == 401


def test_unknown_redirect_uri_is_refused(shop):
    r = shop.get("/oauth/authorize", params={"client_id": "ucp", "redirect_uri": "http://evil.example/cb"})
    assert r.status_code == 400


def test_code_is_single_use_and_refresh_rotates(shop):
    r = shop.post("/oauth/authorize", data={"client_id": "ucp", "redirect_uri": REDIRECT,
                                            "username": "khang", "password": "khang123"}, follow_redirects=False)
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    body = {**CLIENT, "grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT}
    first = shop.post("/oauth/token", data=body)
    assert first.status_code == 200
    assert shop.post("/oauth/token", data=body).status_code == 400

    refresh = {**CLIENT, "grant_type": "refresh_token", "refresh_token": first.json()["refresh_token"]}
    second = shop.post("/oauth/token", data=refresh)
    assert second.status_code == 200
    assert second.json()["refresh_token"] != first.json()["refresh_token"]
    assert shop.post("/oauth/token", data=refresh).status_code == 400  # old one is dead


def test_me_and_address_book(shop):
    tokens = login(shop)
    assert shop.get("/me", headers=auth(tokens)).json()["id"] == "khang"
    addrs = shop.get("/me/addresses", headers=auth(tokens)).json()["items"]
    assert {a["id"] for a in addrs} == {"addr_k1", "addr_k2"}
    assert shop.get("/me").status_code == 401


def test_expired_access_token_is_refused(shop):
    tokens = login(shop)
    shop.post("/_admin/clock", params={"advance_seconds": 3601})
    assert shop.get("/me", headers=auth(tokens)).status_code == 401


# -- holds and stock -----------------------------------------------------


def test_last_unit_second_checkout_gets_out_of_stock(shop):
    a, b = login(shop), login(shop, "linh", "linh123")
    assert checkout(shop, a, "v_lamp").status_code == 201
    r = checkout(shop, b, "v_lamp")
    assert r.status_code == 409 and r.json()["error"]["code"] == "OUT_OF_STOCK"


def test_hold_expiry_returns_stock_and_checkout_expires(shop):
    tokens = login(shop)
    co = checkout(shop, tokens, "v_lamp").json()
    assert shop.get("/products/p_lamp").json()["variants"][0]["available"] == 0
    shop.post("/_admin/clock", params={"advance_seconds": 601})
    assert shop.get("/products/p_lamp").json()["variants"][0]["available"] == 1
    r = place(shop, tokens, co["id"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "CHECKOUT_EXPIRED"


def test_renew_extends_hold(shop):
    tokens = login(shop)
    co = checkout(shop, tokens).json()
    shop.post("/_admin/clock", params={"advance_seconds": 500})
    renewed = shop.post(f"/checkouts/{co['id']}/renew", headers=auth(tokens)).json()
    assert renewed["hold"]["expires_at"] > co["hold"]["expires_at"]
    shop.post("/_admin/clock", params={"advance_seconds": 500})  # past the first expiry, inside the new one
    assert place(shop, tokens, co["id"]).status_code == 201


def test_release_frees_stock(shop):
    tokens = login(shop)
    co = checkout(shop, tokens, "v_lamp").json()
    shop.post(f"/checkouts/{co['id']}/release", headers=auth(tokens))
    assert checkout(shop, tokens, "v_lamp").status_code == 201


def test_without_holds_stock_is_checked_at_place_order(shop):
    shop.post("/_admin/config", json={"supports_holds": False})
    a, b = login(shop), login(shop, "linh", "linh123")
    co_a, co_b = checkout(shop, a, "v_lamp").json(), checkout(shop, b, "v_lamp").json()
    assert co_a["hold"]["held"] is False
    assert place(shop, a, co_a["id"], "ka").status_code == 201
    r = place(shop, b, co_b["id"], "kb")
    assert r.status_code == 409 and r.json()["error"]["code"] == "OUT_OF_STOCK"


def test_checkout_of_another_user_is_not_visible(shop):
    a, b = login(shop), login(shop, "linh", "linh123")
    co = checkout(shop, a).json()
    assert place(shop, b, co["id"]).status_code == 404


# -- orders and idempotency ----------------------------------------------


def test_place_order_returns_payment_url(shop):
    _, _, order = order_flow(shop)
    assert order["status"] == "pending_payment"
    assert order["payment_url"].endswith(f"/pay/{order['id']}")
    assert order["total"] == 90_000 + 20_000


def test_same_key_returns_same_order(shop):
    tokens = login(shop)
    co = checkout(shop, tokens).json()
    first, second = place(shop, tokens, co["id"]).json(), place(shop, tokens, co["id"]).json()
    assert first["id"] == second["id"]
    assert shop.get("/products/p_mug").json()["variants"][0]["available"] == 49  # one unit, not two


def test_same_key_other_checkout_conflicts(shop):
    tokens = login(shop)
    c1, c2 = checkout(shop, tokens).json(), checkout(shop, tokens).json()
    place(shop, tokens, c1["id"])
    assert place(shop, tokens, c2["id"]).json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_place_order_needs_idempotency_key(shop):
    tokens = login(shop)
    co = checkout(shop, tokens).json()
    r = shop.post("/orders", json={"checkout_id": co["id"]}, headers=auth(tokens))
    assert r.status_code == 400


def test_lookup_by_idempotency_key(shop):
    tokens, _, order = order_flow(shop)
    r = shop.get("/orders", params={"idempotency_key": "k1"}, headers=auth(tokens))
    assert r.json()["id"] == order["id"]
    assert shop.get("/orders", params={"idempotency_key": "nope"}, headers=auth(tokens)).status_code == 404


def test_price_change_after_checkout_shows_in_order_total(shop):
    tokens = login(shop)
    co = checkout(shop, tokens).json()
    shop.post("/_admin/variants/v_mug", params={"price": 100_000})
    order = place(shop, tokens, co["id"]).json()
    assert co["total"] == 110_000 and order["total"] == 120_000


# -- payment and webhooks ------------------------------------------------


def test_payment_success_places_order_and_sends_signed_webhook(shop):
    tokens, _, order = order_flow(shop)
    r = shop.post(f"/pay/{order['id']}", content="outcome=auto", headers={"accept": "application/json"})
    assert r.json()["status"] == "placed" and r.json()["payment_status"] == "succeeded"
    event, headers, body = shop.sent[-1]
    assert event["type"] == "order.paid" and event["data"]["order_id"] == order["id"]
    assert headers["X-Mock-Signature"] == sign("s3cret", headers["X-Mock-Timestamp"], body)
    assert shop.get(f"/orders/{order['id']}", headers=auth(tokens)).json()["status"] == "placed"


def test_user_declines_then_retries(shop):
    _, _, order = order_flow(shop)
    pay = lambda outcome: shop.post(f"/pay/{order['id']}", content=f"outcome={outcome}",
                                    headers={"accept": "application/json"}).json()
    assert pay("fail")["payment_status"] == "failed"
    assert pay("auto")["payment_status"] == "succeeded"


def test_mode_always_fail(shop):
    shop.post("/_admin/config", json={"mode": "always_fail"})
    _, _, order = order_flow(shop)
    r = shop.post(f"/pay/{order['id']}", content="outcome=auto", headers={"accept": "application/json"})
    assert r.json()["payment_status"] == "failed" and r.json()["status"] == "pending_payment"


def test_mode_random_follows_seed(shop):
    shop.post("/_admin/config", json={"mode": "random", "seed": 7})
    results = set()
    for i in range(12):
        tokens = login(shop)
        co = checkout(shop, tokens).json()
        o = place(shop, tokens, co["id"], f"k{i}").json()
        results.add(shop.post(f"/pay/{o['id']}", content="outcome=auto",
                              headers={"accept": "application/json"}).json()["payment_status"])
    assert results == {"succeeded", "failed"}


def test_mode_webhook_error_sends_bad_signature(shop):
    shop.post("/_admin/config", json={"mode": "webhook_error"})
    _, _, order = order_flow(shop)
    r = shop.post(f"/pay/{order['id']}", content="outcome=auto", headers={"accept": "application/json"})
    assert r.json()["payment_status"] == "succeeded"
    _, headers, body = shop.sent[-1]
    assert headers["X-Mock-Signature"] != sign("s3cret", headers["X-Mock-Timestamp"], body)


def test_mode_slow_delays_calls_but_not_admin(shop):
    shop.post("/_admin/config", json={"mode": "slow", "slow_delay": 0.3})
    t = time.perf_counter()
    shop.get("/_admin/config")
    assert time.perf_counter() - t < 0.2
    t = time.perf_counter()
    shop.get("/products")
    assert time.perf_counter() - t >= 0.3


def test_invalid_mode_is_rejected(shop):
    assert shop.post("/_admin/config", json={"mode": "bogus"}).status_code == 422


def test_unpaid_order_is_cancelled_at_payment_deadline(shop):
    tokens, _, order = order_flow(shop, "v_lamp")
    shop.post("/_admin/clock", params={"advance_seconds": 1000})  # slow payer, still inside the deadline
    assert shop.get(f"/orders/{order['id']}", headers=auth(tokens)).json()["status"] == "pending_payment"
    assert shop.get("/products/p_lamp").json()["variants"][0]["available"] == 0  # stock still kept
    shop.post("/_admin/clock", params={"advance_seconds": 1000})
    assert shop.get(f"/orders/{order['id']}", headers=auth(tokens)).json()["status"] == "cancelled"
    assert shop.get("/products/p_lamp").json()["variants"][0]["available"] == 1
    assert shop.platform.events[-1]["data"]["reason"] == "payment_timeout"


def test_payment_page_renders_and_pays(shop):
    _, _, order = order_flow(shop)
    assert "Pay" in shop.get(f"/pay/{order['id']}").text
    assert "Payment received" in shop.post(f"/pay/{order['id']}", content="outcome=auto").text


# -- status, cancel, refund ----------------------------------------------


def paid(shop, **kw):
    tokens, co, order = order_flow(shop, **kw)
    shop.post(f"/pay/{order['id']}", content="outcome=auto", headers={"accept": "application/json"})
    return tokens, order


def test_status_transitions_follow_the_table(shop):
    _, order = paid(shop)
    set_status = lambda s: shop.post(f"/_admin/orders/{order['id']}/status", params={"status": s})
    assert set_status("delivered").status_code == 409  # cannot skip
    for s in ("processing", "shipped", "delivered"):
        assert set_status(s).status_code == 200
    assert shop.platform.events[-1]["data"] == {"order_id": order["id"], "status": "delivered", "previous": "shipped"}
    assert set_status("cancelled").status_code == 409


def test_cancel_paid_order_creates_refund(shop):
    tokens, order = paid(shop, variant="v_lamp")
    r = shop.post(f"/orders/{order['id']}/cancel", headers={**auth(tokens), "Idempotency-Key": "c1"}).json()
    assert r["order"]["status"] == "cancelled" and r["refund_ref"]
    assert shop.get("/products/p_lamp").json()["variants"][0]["available"] == 1
    refund = shop.get(f"/refunds/{r['refund_ref']}", headers=auth(tokens)).json()
    assert refund["status"] == "initiated" and refund["amount"] == order["total"]
    shop.post(f"/_admin/refunds/{r['refund_ref']}/settle")
    assert shop.get(f"/refunds/{r['refund_ref']}", headers=auth(tokens)).json()["status"] == "completed"
    assert shop.platform.events[-1]["type"] == "refund.completed"


def test_cancel_unpaid_order_has_no_refund(shop):
    tokens, _, order = order_flow(shop)
    r = shop.post(f"/orders/{order['id']}/cancel", headers={**auth(tokens), "Idempotency-Key": "c1"}).json()
    assert r["order"]["status"] == "cancelled" and r["refund_ref"] is None


def test_cancel_replay_returns_same_result(shop):
    tokens, order = paid(shop)
    h = {**auth(tokens), "Idempotency-Key": "c1"}
    first = shop.post(f"/orders/{order['id']}/cancel", headers=h).json()
    assert shop.post(f"/orders/{order['id']}/cancel", headers=h).json() == first
    again = shop.post(f"/orders/{order['id']}/cancel", headers={**auth(tokens), "Idempotency-Key": "c2"})
    assert again.status_code == 409  # new key on an already cancelled order


def test_cancel_after_shipped_is_refused(shop):
    tokens, order = paid(shop)
    for s in ("processing", "shipped"):
        shop.post(f"/_admin/orders/{order['id']}/status", params={"status": s})
    r = shop.post(f"/orders/{order['id']}/cancel", headers={**auth(tokens), "Idempotency-Key": "c1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "CANNOT_CANCEL"


def test_reset_restores_seed_state(shop):
    paid(shop)
    shop.post("/_admin/reset")
    assert shop.platform.orders == {} and shop.get("/products/p_mug").json()["variants"][0]["available"] == 50


def test_same_variant_twice_cannot_oversell(shop):
    items = [{"variant_id": "v_lamp", "quantity": 1}] * 2  # only 1 lamp in stock
    r = shop.post("/checkouts", json={"items": items}, headers=auth(login(shop)))
    assert r.status_code == 409


def test_admin_config_hides_webhook_secret(shop):
    assert "webhook_secret" not in shop.get("/_admin/config").json()


def test_place_order_replay_is_not_visible_to_another_user(shop):
    khang, linh = login(shop), login(shop, "linh", "linh123")
    co = checkout(shop, khang).json()["id"]
    assert place(shop, khang, co, "k-x").status_code == 201
    assert place(shop, linh, co, "k-x").status_code == 404
