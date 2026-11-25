import asyncio
import json
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import StreamingResponse

from ucp.common.config import get_settings
from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.identity import service as identity
from ucp.modules.notification import service

router = APIRouter()
HEARTBEAT = 15  # seconds of silence before a comment line, so proxies and dead clients are noticed


def reader(authorization: str | None = Header(None)) -> str:
    """Order events need the same right as reading an order (hard rule 11)."""
    agent_id = identity.authenticate(authorization)
    if "view_order" not in identity.permissions(agent_id):
        raise UCPError(ErrorCode.NO_USER_LINK, "link a user account first")
    return agent_id


@router.get("/v1/events")
def replay(after: int = Query(ge=0, le=2**63 - 1), limit: int = Query(100, ge=1, le=500),
           agent_id: str = Depends(reader)) -> dict:
    return {"events": service.events_after(agent_id, after, limit)}


async def _stream(authorization: str, agent_id: str, cursor: int) -> AsyncIterator[str]:
    # shortcut: every open stream polls the log instead of the per-instance fanout queue of addendum C5. Any instance
    # sees every event that way, with no broker. Move to LISTEN/NOTIFY or a broker fanout when polling costs too much.
    s = get_settings()
    end = time.monotonic() + s.sse_max_seconds  # the client reconnects with Last-Event-ID
    sent = time.monotonic()
    yield "retry: 1000\n\n"
    while True:
        try:  # session and link are checked again on every poll: expiry, suspension or unlink ends the stream
            await asyncio.to_thread(reader, authorization)
        except UCPError:
            return
        events = await asyncio.to_thread(service.events_after, agent_id, cursor, 100)
        for e in events:
            cursor = e["seq"]
            sent = time.monotonic()
            yield f"id: {cursor}\ndata: {json.dumps(e)}\n\n"
        if time.monotonic() >= end:
            return
        if time.monotonic() - sent >= HEARTBEAT:
            sent = time.monotonic()
            yield ": ping\n\n"
        await asyncio.sleep(s.sse_poll_seconds)


@router.get("/v1/events/stream")
def stream(authorization: str | None = Header(None), last_event_id: str | None = Header(None),
           agent_id: str = Depends(reader)) -> StreamingResponse:
    """A new stream sends only new events; Last-Event-ID resumes after that seq."""
    if last_event_id is None:
        cursor = service.last_seq(agent_id)
    elif last_event_id.isascii() and last_event_id.isdigit() and len(last_event_id) < 19:
        cursor = int(last_event_id)
    else:
        raise UCPError(ErrorCode.BAD_REQUEST, "Last-Event-ID must be an event seq")
    return StreamingResponse(_stream(authorization, agent_id, cursor), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
