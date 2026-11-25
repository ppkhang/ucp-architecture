import uuid
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.identity.router import current_agent
from ucp.modules.trade import cancel, idempotency, service

router = APIRouter(prefix="/v1")

HANDLERS = {"create_checkout": lambda a, k, p, h: service.create_checkout(a, p, h),
            "select_shipping_method": lambda a, k, p, h: service.select_shipping_method(a, p, h),
            "apply_promotion": lambda a, k, p, h: service.apply_promotion(a, p, h),
            "extend_checkout": lambda a, k, p, h: service.extend_checkout(a, p, h),
            "cancel_checkout": lambda a, k, p, h: service.cancel_checkout(a, p, h),
            "confirm_order": service.confirm_order,
            "cancel_order": cancel.cancel_order}


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
    return await run_in_threadpool(service.record_confirmation, str(checkout_id), form.get("token", [""])[0])


@router.get("/shipping/options")
def shipping_options(request: Request, platform_id: str, city: str = Query(min_length=1, max_length=64),
                     district: str | None = None, agent_id: str = Depends(current_agent)) -> dict:
    # the city is the shop's region; the mock shop does not price by district
    return service.shipping_options(agent_id, platform_id, city, request.app.state.platform_http)


@router.get("/orders/{order_id}")
def get_order(order_id: uuid.UUID, agent_id: str = Depends(current_agent)) -> dict:
    return service.get_order(agent_id, str(order_id))


@router.post("/platform-events/{platform_id}", status_code=202)
async def platform_event(platform_id: str, request: Request) -> Response:
    await run_in_threadpool(service.receive_event, platform_id, request.headers, await request.body())  # blocking DB
    return Response(status_code=202)
