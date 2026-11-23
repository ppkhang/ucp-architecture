import uuid

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ucp.common.errors import ErrorCode, UCPError
from ucp.modules.identity import service

router = APIRouter()


def current_agent(authorization: str | None = Header(None)) -> str:
    return service.authenticate(authorization)


def own_agent(agent_id: uuid.UUID, me: str = Depends(current_agent)) -> str:
    if str(agent_id) != me:
        raise UCPError(ErrorCode.FORBIDDEN, "token belongs to another agent")
    return me


class AgentIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(None, max_length=2000)


class TokenIn(BaseModel):
    grant_type: str = Field(pattern="^api_key$")


class LinkIn(BaseModel):
    platform_id: str


@router.post("/v1/agents", status_code=201)
def register(body: AgentIn) -> dict:
    return service.register_agent(body.name, body.description)


@router.post("/v1/auth/token")
def token(body: TokenIn, x_api_key: str | None = Header(None)) -> dict:
    return service.issue_token(x_api_key)


@router.get("/v1/agents/{agent_id}/permissions")
def permissions(me: str = Depends(own_agent)) -> dict:
    return {"permissions": service.permissions(me)}


@router.post("/v1/agents/{agent_id}/links", status_code=201)
def create_link(body: LinkIn, me: str = Depends(own_agent)) -> dict:
    return service.start_link(me, body.platform_id)


@router.delete("/v1/agents/{agent_id}/links/{link_id}", status_code=204)
def delete_link(link_id: uuid.UUID, me: str = Depends(own_agent)) -> Response:
    service.unlink(me, str(link_id))
    return Response(status_code=204)


@router.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, code: str = "", state: str = "") -> str:
    service.finish_link(state, code, request.app.state.platform_http)
    return "<p>Your account is linked. You can close this page.</p>"
