"""The tools. Descriptions are what the model reads, so they carry the rules it has to follow."""
import time
import uuid
from typing import Any
from urllib.parse import quote

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from mcp_server.ucp_client import UCPClient

INSTRUCTIONS = """Shop through UCP for the user. The only platform is "mock", a test shop where no money moves.

NEVER ask the user to tell you when they have confirmed or paid. Show the link, then wait with a tool call.

1. If a tool answers UCP_NO_USER_LINK or UCP_LINK_EXPIRED, call link_account and give the user the auth_url.
2. search_products and get_product, then create_checkout. Show the user the proposal and confirm_url.
3. In the same reply, after writing the link, call confirm_order with wait_for_user=true. It returns as soon as the
   user agrees on the page. You cannot confirm a purchase, or the cancellation of a paid order: only the user can,
   signed in to the shop. Never say they agreed unless a tool result shows it.
4. confirm_order returns payment_url. Show it, then call get_order with wait_for_payment=true.
5. Call cancel_order only when the user asked you to cancel. An unpaid order is cancelled at once: if the result
   has confirmed_by_user false, tell the user plainly that it was cancelled without a confirmation page. A paid
   order answers UCP_CONFIRMATION_REQUIRED with details.confirm_url: write that link, then call cancel_order again
   with wait_for_user=true. The shop may refuse (UCP_CANNOT_CANCEL). After every successful cancel, ALWAYS show
   the user the order_url from the result, so they can see the cancelled order.
6. If a wait ends and the user has not acted, call it again. After 4 waits in a row, stop and say you will check
   when they next write to you.
7. Put every link (auth_url, confirm_url, payment_url, order_url) alone on its own line, exactly as given: no
   brackets, quotes, bold or punctuation around it, or the link breaks.
8. If a call fails because UCP or the shop did not answer, retry with the idempotency_key named in the error.
   Use a new key (leave it out) for a new request."""

# shortcut: a held tool call is the only way to hear back inside one reply. Kept under the ~100 s a tunnel allows.
WAIT_SECONDS, POLL_SECONDS = 50, 2

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
    def get_order(order_id: str, wait_for_payment: bool = False) -> dict:
        """The order's current status. wait_for_payment holds the call until the order leaves pending_payment, so
        use it right after you have shown the payment link."""
        end = time.monotonic() + (WAIT_SECONDS if wait_for_payment else 0)
        while True:
            order = ucp.call("GET", f"/v1/orders/{quote(order_id, safe='')}")
            if order["status"] != "pending_payment" or time.monotonic() >= end:
                break
            time.sleep(POLL_SECONDS)
        if wait_for_payment and order["status"] == "pending_payment":
            order["waiting"] = "Not paid yet. Call get_order with wait_for_payment=true again."
        return order

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

    def when_the_user_agrees(method: str, params: dict, key: str | None, wait: bool) -> dict:
        """Send the intent, and while UCP says the user has not agreed, try again until they do or time is up.
        UCP decides every time: waiting cannot stand in for the user."""
        key = key or str(uuid.uuid4())  # one key throughout: a refused call frees it, a success is replayed
        end = time.monotonic() + (WAIT_SECONDS if wait else 0)
        while True:
            try:
                return ucp.intent(method, params, key)
            except ToolError as e:
                if "UCP_CONFIRMATION_REQUIRED" not in str(e):
                    raise
                if time.monotonic() >= end:
                    if wait:
                        raise ToolError(f"{e}\nThe user has not agreed yet. Call again with wait_for_user=true.") from None
                    raise
            time.sleep(POLL_SECONDS)

    @mcp.tool()
    def confirm_order(checkout_id: str, wait_for_user: bool = False, idempotency_key: str | None = None) -> dict:
        """Place the order the user agrees to on the confirmation page. Fails if they have not. After showing
        confirm_url, call with wait_for_user=true: it returns as soon as they agree, so do not ask them to tell you."""
        return when_the_user_agrees("confirm_order", {"checkout_id": checkout_id}, idempotency_key, wait_for_user)

    @mcp.tool(annotations=ToolAnnotations(destructiveHint=True))
    def cancel_order(order_id: str, reason: str | None = None, wait_for_user: bool = False,
                     idempotency_key: str | None = None) -> dict:
        """Cancel an order, only when the user asked. Unpaid: cancelled at once, tell the user. Paid: the first call
        returns a confirm_url. Show it, then call again with wait_for_user=true: it finishes as soon as they agree."""
        params = {"order_id": order_id} | ({"reason": reason} if reason else {})
        result = when_the_user_agrees("cancel_order", params, idempotency_key, wait_for_user)
        if result.get("order_url"):  # said again in the result itself, where the model reads it last
            result["show_the_user"] = ("In your reply, give the user this link to the cancelled order, alone on its "
                                       f"own line:\n{result['order_url']}")
        return result

    return mcp
