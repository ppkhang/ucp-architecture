"""Adapter for the mock shop (mock_platform/). Maps its shapes to UCP's."""
from typing import Any

import httpx

from ucp.adapters.base import PlatformTimeout
from ucp.common.errors import ErrorCode, UCPError

TIMEOUT = 3.0  # own timeout per adapter, so a slow shop cannot hold a request for long
# shortcut: no circuit breaker yet; add one per adapter before a second platform exists


# platform error code -> UCP error code; anything else is PLATFORM_UNAVAILABLE
ERRORS = {
    "OUT_OF_STOCK": ErrorCode.INSUFFICIENT_STOCK,
    "CHECKOUT_EXPIRED": ErrorCode.CHECKOUT_EXPIRED,
    "NOT_FOUND": ErrorCode.NOT_FOUND,
    "INVALID_REQUEST": ErrorCode.BAD_REQUEST,
    "IDEMPOTENCY_CONFLICT": ErrorCode.IDEMPOTENCY_CONFLICT,
    "CANNOT_CANCEL": ErrorCode.CANNOT_CANCEL,
    "ORDER_PAID": ErrorCode.CONFIRMATION_REQUIRED,
}


class MockPlatformAdapter:
    platform_id = "mock"

    def __init__(self, http: httpx.Client, base_url: str):
        self.http, self.base = http, base_url.rstrip("/")

    def _call(self, method: str, path: str, *, token: str | None = None, json: Any = None,
              params: dict[str, Any] | None = None, key: str | None = None) -> Any:
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if key:
            headers["Idempotency-Key"] = key
        try:
            r = self.http.request(method, self.base + path, json=json, params=params, headers=headers,
                                  timeout=TIMEOUT)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, "platform did not answer") from None  # never reached it
        except httpx.HTTPError:
            raise PlatformTimeout() from None
        if r.status_code < 300:
            return r.json()
        if r.status_code == 401 and token:
            raise UCPError(ErrorCode.LINK_EXPIRED, "platform token no longer valid")
        try:
            err = r.json()["error"]
        except (ValueError, KeyError, TypeError):
            err = {}
        raise UCPError(ERRORS.get(err.get("code"), ErrorCode.PLATFORM_UNAVAILABLE),
                       err.get("message") or f"platform answered {r.status_code}")

    def capabilities(self) -> dict[str, Any]:
        return self._call("GET", "/capabilities")

    def search_products(self, query: str) -> list[dict[str, Any]]:
        return [_product(p) for p in self._call("GET", "/products", params={"q": query})["items"]]

    def get_product(self, product_id: str) -> dict[str, Any]:
        return _product(self._call("GET", f"/products/{product_id}"))

    @staticmethod
    def _items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"variant_id": i["variant_id"], "quantity": i["qty"]} for i in items]

    def _basket(self, items: list[dict[str, Any]], region: str, shipping_method_id: str | None,
                promo_code: str | None) -> dict[str, Any]:
        return {"items": self._items(items), "region": region, "shipping_method_id": shipping_method_id,
                "promo_code": promo_code}

    def get_quote(self, token: str, items: list[dict[str, Any]], region: str, shipping_method_id: str | None = None,
                  promo_code: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/quote", json=self._basket(items, region, shipping_method_id, promo_code))

    def create_checkout(self, token: str, items: list[dict[str, Any]], region: str,
                        shipping_method_id: str | None = None, promo_code: str | None = None) -> dict[str, Any]:
        return self._call("POST", "/checkouts", token=token,
                          json=self._basket(items, region, shipping_method_id, promo_code))

    def shipping_options(self, region: str) -> list[dict[str, Any]]:
        return [{"method_id": o["id"], "name": o["name"], "fee": o["fee"], "eta_days": o["eta_days"]}
                for o in self._call("GET", "/shipping/options", params={"region": region})["items"]]

    def renew_hold(self, token: str, platform_checkout_id: str) -> dict[str, Any]:
        return self._call("POST", f"/checkouts/{platform_checkout_id}/renew", token=token)

    def cancel_order(self, token: str, platform_order_id: str, idempotency_key: str,
                     only_if_unpaid: bool = False) -> dict[str, Any]:
        return self._call("POST", f"/orders/{platform_order_id}/cancel", token=token, key=idempotency_key,
                          params={"only_if_unpaid": "true"} if only_if_unpaid else None)

    def place_order(self, token: str, checkout_id: str, idempotency_key: str) -> dict[str, Any]:
        return self._call("POST", "/orders", token=token, json={"checkout_id": checkout_id}, key=idempotency_key)

    def get_order(self, token: str, platform_order_id: str) -> dict[str, Any]:
        return self._call("GET", f"/orders/{platform_order_id}", token=token)

    def release_hold(self, token: str, platform_checkout_id: str) -> None:
        self._call("POST", f"/checkouts/{platform_checkout_id}/release", token=token)

    def get_order_by_key(self, token: str, idempotency_key: str) -> dict[str, Any] | None:
        try:
            return self._call("GET", "/orders", token=token, params={"idempotency_key": idempotency_key})
        except UCPError as e:
            if e.code == ErrorCode.NOT_FOUND:
                return None
            raise


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
