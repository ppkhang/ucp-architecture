"""PlatformAdapter contract (AGENTS.md section 6). Methods are added as the build steps need them.

Calls made for a user carry that user's platform access token."""
from typing import Any, Protocol

from ucp.common.errors import ErrorCode, UCPError


class PlatformTimeout(UCPError):
    """The request may have reached the platform but the answer was lost. Never resend blindly (hard rule 5)."""

    def __init__(self) -> None:
        super().__init__(ErrorCode.PLATFORM_UNAVAILABLE, "platform did not answer in time")


class PlatformAdapter(Protocol):
    platform_id: str

    def capabilities(self) -> dict[str, Any]: ...
    def search_products(self, query: str) -> list[dict[str, Any]]: ...
    def get_product(self, product_id: str) -> dict[str, Any]: ...
    def get_quote(self, token: str, items: list[dict[str, Any]], region: str) -> dict[str, Any]: ...
    def create_checkout(self, token: str, items: list[dict[str, Any]], region: str) -> dict[str, Any]: ...
    def place_order(self, token: str, checkout_id: str, idempotency_key: str) -> dict[str, Any]: ...
    def get_order_by_key(self, token: str, idempotency_key: str) -> dict[str, Any] | None: ...
