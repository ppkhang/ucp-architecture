"""PlatformAdapter contract (AGENTS.md section 6). Methods are added as the build steps need them."""
from typing import Any, Protocol


class PlatformAdapter(Protocol):
    platform_id: str

    def capabilities(self) -> dict[str, Any]: ...
    def search_products(self, query: str) -> list[dict[str, Any]]: ...
    def get_product(self, product_id: str) -> dict[str, Any]: ...
