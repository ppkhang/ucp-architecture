"""The tools. Descriptions are what the model reads, so they carry the rules it has to follow."""
from typing import Any
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_server.ucp_client import UCPClient

INSTRUCTIONS = """Shop through UCP for the user. The only platform is "mock", a test shop where no money moves.

1. If a tool answers UCP_NO_USER_LINK or UCP_LINK_EXPIRED, call link_account and give the user the auth_url.
2. search_products and get_product, then create_checkout. Show the user the proposal and give them confirm_url.
3. You cannot confirm a purchase or a cancellation. Only the user can, on that page, signed in to the shop.
   Never say they agreed. Wait for them to tell you, then call confirm_order.
4. confirm_order returns payment_url. The user pays there. get_events or get_order shows when it is paid.
5. cancel_order first answers UCP_CONFIRMATION_REQUIRED with details.confirm_url. Give it to the user, wait,
   then call cancel_order again. The shop may still refuse (UCP_CANNOT_CANCEL).
6. If a call fails because UCP or the shop did not answer, retry with the idempotency_key named in the error.
   Use a new key (leave it out) for a new request."""

READ = ToolAnnotations(readOnlyHint=True)


class Item(BaseModel):
    product_id: str
    variant_id: str | None = Field(None, description="Required when the product has more than one variant")
    qty: int = Field(ge=1, le=100)


def build_server(ucp: UCPClient, **settings: Any) -> MCPServer:
    mcp = MCPServer("UCP shop", instructions=INSTRUCTIONS, **settings)

    # -- reads -------------------------------------------------------------

    @mcp.tool(annotations=READ)
    def account_status() -> dict:
        """What this agent may do now. Order scopes appear only once a shop account is linked."""
        return ucp.call("GET", f"/v1/agents/{ucp.agent_id()}/permissions")

    @mcp.tool()
    def link_account(platform_id: str = "mock") -> dict:
        """Start linking the user's shop account. Give the user auth_url: they sign in at the shop."""
        return ucp.call("POST", f"/v1/agents/{ucp.agent_id()}/links", json={"platform_id": platform_id})

    @mcp.tool(annotations=READ)
    def search_products(query: str = "", platform_id: str = "mock", max_price: int | None = None) -> dict:
        """Search the shop. An empty query lists everything. Prices are VND."""
        params = {"platform_id": platform_id, "q": query}
        if max_price is not None:
            params["max_price"] = max_price
        return ucp.call("GET", "/v1/products", params=params)

    @mcp.tool(annotations=READ)
    def get_product(product_id: str, platform_id: str = "mock") -> dict:
        """One product with its variants and prices."""
        return ucp.call("GET", f"/v1/products/{quote(product_id, safe='')}", params={"platform_id": platform_id})

    @mcp.tool(annotations=READ)
    def shipping_options(city: str = "HCM", platform_id: str = "mock") -> dict:
        """Shipping methods and fees. city is the shop's region, such as HCM or HN."""
        return ucp.call("GET", "/v1/shipping/options", params={"platform_id": platform_id, "city": city})

    @mcp.tool(annotations=READ)
    def get_order(order_id: str) -> dict:
        """The order's current status."""
        return ucp.call("GET", f"/v1/orders/{quote(order_id, safe='')}")

    @mcp.tool(annotations=READ)
    def get_events(after: int = 0, limit: int = 50) -> dict:
        """What happened since event number `after`, such as OrderPlaced. Remember the highest seq you saw."""
        return ucp.call("GET", "/v1/events", params={"after": after, "limit": limit})

    # -- one tool per intent ------------------------------------------------

    @mcp.tool()
    def create_checkout(items: list[Item], platform_id: str = "mock", region: str = "HCM",
                        idempotency_key: str | None = None) -> dict:
        """Price a basket at the shop and hold it. Nothing is bought. Show the user the proposal and confirm_url."""
        return ucp.intent("create_checkout", {"platform_id": platform_id, "region": region,
                                              "items": [i.model_dump(exclude_none=True) for i in items]},
                          idempotency_key)

    @mcp.tool()
    def select_shipping_method(checkout_id: str, shipping_method_id: str, idempotency_key: str | None = None) -> dict:
        """Change shipping. The shop prices it again and the user has to confirm the new proposal."""
        return ucp.intent("select_shipping_method",
                          {"checkout_id": checkout_id, "shipping_method_id": shipping_method_id}, idempotency_key)

    @mcp.tool()
    def apply_promotion(checkout_id: str, code: str, idempotency_key: str | None = None) -> dict:
        """Apply a promotion code. The user has to confirm the new proposal."""
        return ucp.intent("apply_promotion", {"checkout_id": checkout_id, "code": code}, idempotency_key)

    @mcp.tool()
    def extend_checkout(checkout_id: str, idempotency_key: str | None = None) -> dict:
        """Give the user more time. Works once, and never past the shop's hold."""
        return ucp.intent("extend_checkout", {"checkout_id": checkout_id}, idempotency_key)

    @mcp.tool()
    def cancel_checkout(checkout_id: str, idempotency_key: str | None = None) -> dict:
        """Drop a checkout that was not ordered and release the shop's hold."""
        return ucp.intent("cancel_checkout", {"checkout_id": checkout_id}, idempotency_key)

    @mcp.tool()
    def confirm_order(checkout_id: str, idempotency_key: str | None = None) -> dict:
        """Place the order the user already agreed to on the confirmation page. Fails if they have not."""
        return ucp.intent("confirm_order", {"checkout_id": checkout_id}, idempotency_key)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    def cancel_order(order_id: str, reason: str | None = None, idempotency_key: str | None = None) -> dict:
        """Cancel an order. The first call returns a confirm_url for the user; call again after they agree."""
        params = {"order_id": order_id} | ({"reason": reason} if reason else {})
        return ucp.intent("cancel_order", params, idempotency_key)

    return mcp
