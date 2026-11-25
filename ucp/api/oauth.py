"""The platform sends users back here after login. The state says who asked: a link (identity) or a confirmation (trade)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ucp.common.links import trim_link
from ucp.modules.identity import service as identity
from ucp.modules.trade import service as trade

router = APIRouter()


@router.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, code: str = "", state: str = "") -> str:
    state = trim_link(state)  # the last parameter of auth_url
    http = request.app.state.platform_http
    if state.startswith("c_"):
        return trade.confirm_login(state, code, http)
    identity.finish_link(state, code, http)
    return "<p>Your account is linked. You can close this page.</p>"
