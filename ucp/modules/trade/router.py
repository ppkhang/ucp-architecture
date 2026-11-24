import uuid
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.identity.router import current_agent
from ucp.modules.trade import idempotency, service

router = APIRouter(prefix="/v1")

# shortcut: the other intents (select_shipping_method, apply_promotion, cancel_checkout, extend_checkout,
# cancel_order) arrive in step 9
HANDLERS = {"create_checkout": lambda a, k, p, h: service.create_checkout(a, p, h),
            "confirm_order": service.confirm_order}


class IntentIn(BaseModel):
    method: str
    params: dict = {}


@router.post("/intent")
def intent(body: IntentIn, request: Request, agent_id: str = Depends(current_agent),
           idempotency_key: str | None = Header(None)) -> dict:
    if not idempotency_key or len(idempotency_key) > 255:
        raise UCPError(ErrorCode.BAD_REQUEST, "Idempotency-Key header is required (1 to 255 characters)")
    handler = HANDLERS.get(body.method)
    if not handler:
        raise UCPError(ErrorCode.BAD_REQUEST, f"method {body.method} is not available")
    http = request.app.state.platform_http
    return idempotency.run(agent_id, idempotency_key, body.model_dump(),
                           lambda: handler(agent_id, idempotency_key, body.params, http))


@router.get("/confirm/{checkout_id}")
def confirm_page(checkout_id: uuid.UUID) -> RedirectResponse:
    return RedirectResponse(service.start_confirmation(str(checkout_id)), status_code=302)


@router.post("/confirm/{checkout_id}", response_class=HTMLResponse)
async def confirm_submit(checkout_id: uuid.UUID, request: Request) -> str:
    form = parse_qs((await request.body()).decode())
    return service.record_confirmation(str(checkout_id), form.get("token", [""])[0])


@router.get("/orders/{order_id}")
def get_order(order_id: uuid.UUID, agent_id: str = Depends(current_agent)) -> dict:
    return service.get_order(agent_id, str(order_id))


@router.post("/platform-events/{platform_id}", status_code=202)
async def platform_event(platform_id: str, request: Request) -> Response:
    service.receive_event(platform_id, request.headers, await request.body())
    return Response(status_code=202)
