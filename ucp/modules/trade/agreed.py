"""What happens when the user presses Agree: UCP does what they agreed to, without waiting for the agent (ADR-007,
amended 2026-11-25). It goes through the same confirm_order and cancel_order as the agent's intents, so the same
checks apply, a second click replays the result, and a lost answer is looked up before anything is sent again.

Only a signed confirmation token gets here, which needs the user's own shop sign-in. The agent cannot produce one."""
from html import escape

import httpx

from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.trade import cancel, service

BACK = "<p>You can go back to your assistant. It can see this result.</p>"


def finish(proposal_id: str, http: httpx.Client) -> tuple[int, str, str | None]:
    """Returns (HTTP status, page, address to send the browser to instead of the page)."""
    try:
        co = service._checkout(proposal_id)
    except UCPError:
        return _cancel(proposal_id, http)
    return _buy(co, http)


def _buy(co: dict, http: httpx.Client) -> tuple[int, str, str | None]:
    checkout_id = str(co["id"])
    try:
        order = service.confirm_order(str(co["agent_id"]), f"page-{checkout_id}", {"checkout_id": checkout_id}, http)
    except UCPError as e:
        placed = service._order_by_checkout(checkout_id)
        if e.code == ErrorCode.CONFIRMATION_REQUIRED and placed:  # the agent or a second click got there first
            order = {"status": placed["status"], "payment_url": placed["payment_url"]}
        elif placed and placed["reconciling"]:
            return e.status, service.page("Checking with the shop", (
                "<p>The shop did not answer in time. UCP is checking whether the order went through. "
                "<b>Do not buy it again.</b></p>") + BACK), None
        else:
            return e.status, _not_done("The order was not placed", e), None
    if order["status"] == "pending_payment" and order["payment_url"]:
        return 303, "", order["payment_url"]
    return 200, service.page("Order placed", f"<p>Status: {escape(order['status'].replace('_', ' '))}</p>" + BACK), None


def _cancel(request_id: str, http: httpx.Client) -> tuple[int, str, str | None]:
    req = service._cancel_request(request_id)
    try:
        result = cancel.cancel_order(str(req["agent_id"]), f"page-{request_id}", {"order_id": str(req["order_id"])}, http)
    except UCPError as e:
        return e.status, _not_done("The order was not cancelled", e), None
    link = (f"<p><a href='{escape(result['order_url'])}'>See the cancelled order</a></p>"
            if result.get("order_url") else "")
    refund = f"<p>Refund {escape(result['refund_ref'])} was started.</p>" if result["refund_ref"] else ""
    return 200, service.page("Order cancelled", refund + link + BACK), None


def _not_done(title: str, e: UCPError) -> str:
    return service.page(title, f"<p>{escape(e.message)}</p><p>Ask your assistant what to do next.</p>")
