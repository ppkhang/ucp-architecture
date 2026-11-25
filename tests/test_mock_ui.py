"""The shop's own pages: they load, the data routes behind them, and the browser sign-in."""
import re
from urllib.parse import parse_qs, urlparse

from tests.test_mock_platform import REDIRECT, shop  # noqa: F401  (the fixture)

ASK = {"client_id": "ucp", "redirect_uri": REDIRECT, "state": "s1"}
PASSWORD = {"username": "khang", "password": "khang123"}


def test_pages_and_assets_load(shop):
    assert "Mock shop" in shop.get("/").text
    for path in ("/static/shop.css", "/static/shop.js", "/static/fonts/rubik-latin.woff2"):
        assert shop.get(path).status_code == 200


def test_state_holds_no_secrets(shop):
    shop.post("/shop/session", json=PASSWORD)
    state = shop.get("/_admin/state").json()
    assert {p["id"] for p in state["products"]} >= {"p_mug", "p_lamp"} and "webhook_secret" not in str(state)
    assert "password" not in str(state) and "token" not in str(state) and "csrf" not in str(state)


def test_my_orders_follows_the_cookie(shop):
    assert shop.get("/shop/session").json() == {"user": None, "items": [], "alerts": []}
    assert shop.post("/shop/session", json={**PASSWORD, "password": "nope"}).status_code == 401
    r = shop.post("/shop/session", json=PASSWORD)
    cookie = r.headers["set-cookie"].lower()
    assert r.status_code == 204 and "httponly" in cookie and "samesite=lax" in cookie
    assert shop.get("/shop/session").json()["user"] == "khang"
    assert shop.delete("/shop/session").status_code == 204
    assert shop.get("/shop/session").json()["user"] is None


def authorize(shop, **data):
    return shop.post("/oauth/authorize", data={**ASK, **data}, follow_redirects=False)


def continue_page(shop):
    page = shop.get("/oauth/authorize", params=ASK)
    assert page.headers["x-frame-options"] == "DENY"
    return page.text


def test_after_one_sign_in_the_shop_offers_continue(shop):
    assert "type=password" in continue_page(shop)
    assert authorize(shop, **PASSWORD).status_code == 302
    page = continue_page(shop)
    assert "Continue as khang" in page and "type=password" not in page
    csrf = re.search(r"name=csrf value='([^']+)'", page).group(1)
    r = authorize(shop, action="continue", csrf=csrf)
    back = parse_qs(urlparse(r.headers["location"]).query)
    assert r.status_code == 302 and back["state"] == ["s1"] and back["code"]


def test_continue_needs_the_pages_own_token(shop):
    authorize(shop, **PASSWORD)
    for extra in ({}, {"csrf": "guess"}):  # what another site could send
        r = authorize(shop, action="continue", **extra)
        assert r.status_code == 401 and "location" not in r.headers


def test_switching_account_and_expiry_sign_out(shop):
    authorize(shop, **PASSWORD)
    old = shop.cookies.get("mock_shop_session")
    assert "type=password" in authorize(shop, action="switch").text
    shop.cookies.set("mock_shop_session", old)  # the old cookie is dead on the server too
    assert "type=password" in continue_page(shop)
    shop.cookies.clear()
    authorize(shop, **PASSWORD)
    shop.post("/_admin/clock", params={"advance_seconds": 3601})
    assert "type=password" in continue_page(shop)


def test_a_user_sees_and_dismisses_only_their_own_alerts(shop):
    alerts = shop.app.state.platform.alerts
    for user in ("khang", "linh"):
        alerts.append({"user": user, "order_id": f"ord_{user}", "total": 1, "created_at": "x", "seen": False})
    shop.delete("/shop/alerts")  # signed out: changes nothing
    shop.post("/shop/session", json=PASSWORD)
    assert [a["order_id"] for a in shop.get("/shop/session").json()["alerts"]] == ["ord_khang"]
    assert shop.delete("/shop/alerts").status_code == 204
    assert shop.get("/shop/session").json()["alerts"] == [] and not alerts[1]["seen"]


def test_only_the_sign_in_only_client_skips_continue(shop):
    authorize(shop, **PASSWORD)
    page = shop.get("/oauth/authorize", params={**ASK, "client_id": "ucp-confirm"}, follow_redirects=False)
    assert page.status_code == 302 and page.headers["location"].startswith(REDIRECT)
    linking = shop.get("/oauth/authorize", params=ASK, follow_redirects=False)
    assert linking.status_code == 200 and "Continue as khang" in linking.text
    shop.cookies.clear()
    signed_out = shop.get("/oauth/authorize", params={**ASK, "client_id": "ucp-confirm"}, follow_redirects=False)
    assert signed_out.status_code == 200 and "type=password" in signed_out.text

