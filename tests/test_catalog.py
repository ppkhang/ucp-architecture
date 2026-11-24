"""Step 5: catalog (UC-01) through the mock adapter."""
import httpx

from ucp.api.main import app
from tests.conftest import agent  # noqa: F401  (ucp is a fixture from conftest)


def get(c, path, auth, **params):
    return c.get(path, params={"platform_id": "mock", **params}, headers=auth)


def test_search_maps_shop_products(ucp):
    _, _, auth = agent(ucp)
    r = get(ucp, "/v1/products", auth, q="shoes")
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 1 and body["partial"] is False
    assert body["products"][0] == {"product_id": "p_shoes", "platform_id": "mock", "name": "Running shoes",
                                   "selling_price": 1_200_000, "currency": "VND", "rating": None,
                                   "availability_type": "in_stock"}


def test_search_filters_and_pages(ucp):
    _, _, auth = agent(ucp)
    cheap = get(ucp, "/v1/products", auth, max_price=200_000).json()
    assert {p["product_id"] for p in cheap["products"]} == {"p_tee", "p_mug"}
    page2 = get(ucp, "/v1/products", auth, limit=4, page=2).json()
    assert page2["total_count"] == 6 and len(page2["products"]) == 2


def test_detail_price_stock(ucp):
    _, _, auth = agent(ucp)
    p = get(ucp, "/v1/products/p_tee", auth).json()
    assert p["availability_type"] == "partial"  # one variant sold out
    assert [v["variant_id"] for v in p["variants"]] == ["v_tee_m_black", "v_tee_l_black", "v_tee_m_white"]
    assert get(ucp, "/v1/products/p_tee/price", auth).json() == {
        "list_price": 150_000, "selling_price": 150_000, "currency": "VND"}
    assert get(ucp, "/v1/products/p_lamp/stock", auth).json() == {
        "available_qty": 1, "availability_type": "in_stock", "max_per_user": None}


def test_stock_is_live_from_platform(ucp):
    _, _, auth = agent(ucp)
    app.state.platform_http.post("/_admin/variants/v_lamp", params={"stock": 0})
    assert get(ucp, "/v1/products/p_lamp/stock", auth).json()["availability_type"] == "out_of_stock"


def test_errors(ucp):
    _, _, auth = agent(ucp)
    assert get(ucp, "/v1/products/nope", auth).json()["error"]["code"] == "UCP_NOT_FOUND"
    assert get(ucp, "/v1/products", auth, platform_id="shopee").json()["error"]["code"] == "UCP_NOT_FOUND"
    assert ucp.get("/v1/products", headers=auth).json()["error"]["code"] == "UCP_BAD_REQUEST"  # platform_id required
    assert ucp.get("/v1/products", params={"platform_id": "mock"}).status_code == 401


def test_platform_down_is_503(ucp):
    _, _, auth = agent(ucp)
    def down(request):
        raise httpx.ConnectTimeout("slow", request=request)
    app.state.platform_http = httpx.Client(transport=httpx.MockTransport(down))
    r = get(ucp, "/v1/products", auth)
    assert (r.status_code, r.json()["error"]["code"]) == (503, "UCP_PLATFORM_UNAVAILABLE")
