"""Adapter for the mock shop (mock_platform/). Maps its shapes to UCP's."""
from typing import Any

import httpx

from ucp.common.errors import ErrorCode, UCPError

TIMEOUT = 3.0  # own timeout per adapter, so a slow shop cannot hold a request for long
# shortcut: no circuit breaker yet; add one per adapter before a second platform exists


class MockPlatformAdapter:
    platform_id = "mock"

    def __init__(self, http: httpx.Client, base_url: str):
        self.http, self.base = http, base_url.rstrip("/")

    def _get(self, path: str, **params: Any) -> dict[str, Any]:
        try:
            r = self.http.get(self.base + path, params=params, timeout=TIMEOUT)
        except httpx.HTTPError:
            raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, "platform did not answer") from None
        if r.status_code == 404:
            raise UCPError(ErrorCode.NOT_FOUND, "product not found")
        if r.status_code != 200:
            raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, f"platform answered {r.status_code}")
        return r.json()

    def capabilities(self) -> dict[str, Any]:
        return self._get("/capabilities")

    def search_products(self, query: str) -> list[dict[str, Any]]:
        return [_product(p) for p in self._get("/products", q=query)["items"]]

    def get_product(self, product_id: str) -> dict[str, Any]:
        return _product(self._get(f"/products/{product_id}"))


def _product(p: dict[str, Any]) -> dict[str, Any]:
    variants = p["variants"]
    in_stock = [v for v in variants if v["available"] > 0]
    availability = ("out_of_stock" if not in_stock else "in_stock" if len(in_stock) == len(variants) else "partial")
    return {
        "product_id": p["id"],
        "platform_id": "mock",
        "name": p["name"],
        "description": p["description"],
        "selling_price": min(v["price"] for v in variants),  # "from" price; the variant decides at checkout
        "currency": "VND",
        "rating": None,
        "availability_type": availability,
        "available_qty": sum(v["available"] for v in variants),
        "variants": [{"variant_id": v["id"], "name": v["name"], "selling_price": v["price"],
                      "available_qty": v["available"]} for v in variants],
    }
