"""The shop's own pages: they load, and the data routes behind them."""
from tests.test_mock_platform import shop  # noqa: F401  (the fixture)


def test_pages_and_assets_load(shop):
    assert "Mock shop" in shop.get("/").text
    for path in ("/static/shop.css", "/static/shop.js", "/static/fonts/barlow-400-latin.woff2"):
        assert shop.get(path).status_code == 200


def test_state_and_my_orders(shop):
    assert shop.get("/_admin/state").json()["orders"] == []
    assert shop.post("/shop/my-orders", json={"username": "khang", "password": "nope"}).status_code == 401
    state = shop.get("/_admin/state").json()
    assert {p["id"] for p in state["products"]} >= {"p_mug", "p_lamp"} and "webhook_secret" not in str(state)
    assert "password" not in str(state) and "token" not in str(state)
