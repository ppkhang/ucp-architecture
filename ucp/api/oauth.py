"""The platform sends users back here after login. The state says who asked: a link (identity) or a confirmation (trade)."""
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ucp.modules.identity import service as identity
from ucp.modules.trade import service as trade

router = APIRouter()


@router.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(request: Request, code: str = "", state: str = "") -> str:
    # A link written inside brackets or a sentence picks up the punctuation after it. State is the last
    # parameter, and a real one never ends in any of these.
    state = state.rstrip(")]}>.,;:!?'\"`* ")
    http = request.app.state.platform_http
    if state.startswith("c_"):
        return trade.confirm_login(state, code, http)
    identity.finish_link(state, code, http)
    return "<p>Your account is linked. You can close this page.</p>"
