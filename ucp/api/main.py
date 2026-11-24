from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from ucp.common.db import apply_schemas
from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.catalog.router import router as catalog_router
from ucp.modules.identity.router import router as identity_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_schemas()
    if not hasattr(app.state, "platform_http"):  # tests inject their own client
        app.state.platform_http = httpx.Client(timeout=10)
    yield


app = FastAPI(title="UCP", version="0.1.0", lifespan=lifespan)
app.include_router(identity_router)
app.include_router(catalog_router)


@app.exception_handler(UCPError)
async def ucp_error(_: Request, e: UCPError) -> JSONResponse:
    return JSONResponse(e.body(), status_code=e.status)


@app.exception_handler(RequestValidationError)
async def bad_request(_: Request, e: RequestValidationError) -> JSONResponse:
    err = UCPError(ErrorCode.BAD_REQUEST, "invalid request", {"errors": [{k: x[k] for k in ("loc", "msg", "type")} for x in e.errors()]})
    return JSONResponse(err.body(), status_code=err.status)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
