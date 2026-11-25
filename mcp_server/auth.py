"""OAuth for the HTTP transport: one owner, one password. The SDK serves /register, /authorize and /token and
checks PKCE; this stores what it issues and asks for the password.

shortcut: everything is in memory, so a restart means adding the connector again. Persist it when the server
runs unattended (step 11)."""
import hmac
import html
import secrets
import time
from urllib.parse import parse_qs, urlparse

from mcp.server.auth.provider import (AccessToken, AuthorizationCode, AuthorizationParams, AuthorizeError,
                                      RefreshToken, RegistrationError, TokenError, construct_redirect_uri)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "ucp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
LOGIN_TTL, CODE_TTL, ACCESS_TTL, REFRESH_TTL = 600, 300, 3600, 30 * 86400
MAX_CLIENTS, MAX_PENDING = 100, 100
MAX_FAILURES, LOCKOUT = 5, 600
HEADERS = {"Cache-Control": "no-store", "X-Frame-Options": "DENY", "Referrer-Policy": "no-referrer",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"}


def allowed_redirect(uri: str) -> bool:
    """Registration is open to anyone, so codes may only go to Claude or to an app on the user's own machine.
    Otherwise a stranger could register their own address and trick the owner into signing in for them."""
    if uri == CLAUDE_CALLBACK:
        return True
    u = urlparse(uri)
    return u.scheme == "http" and u.hostname in ("localhost", "127.0.0.1") and u.path == "/callback" and not u.username


class OwnerAuth:
    def __init__(self, public_url: str, password: str):
        self.public_url, self._password = public_url.rstrip("/"), password
        self.resource = f"{self.public_url}/mcp"
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.pending: dict[str, tuple[str, AuthorizationParams, float]] = {}
        self.codes: dict[str, AuthorizationCode] = {}
        self.access: dict[str, AccessToken] = {}
        self.refresh: dict[str, RefreshToken] = {}
        self.failures: list[float] = []

    # -- what the SDK calls --------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or not all(allowed_redirect(u) for u in uris):
            raise RegistrationError("invalid_redirect_uri", "only Claude's callback or a local app may sign in here")
        while len(self.clients) >= MAX_CLIENTS:
            self.clients.pop(next(iter(self.clients)))  # oldest first, so open registration cannot fill memory
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.resource:
            raise AuthorizeError("invalid_target", "this server only issues tokens for itself")
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if v[2] > now}
        while len(self.pending) >= MAX_PENDING:
            self.pending.pop(next(iter(self.pending)))
        txn = secrets.token_urlsafe(32)
        self.pending[txn] = (client.client_id, params, now + LOGIN_TTL)
        return f"{self.public_url}/login?txn={txn}"

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        if not self.codes.pop(authorization_code.code, None):  # single use
            raise TokenError("invalid_grant", "authorization code was already used")
        return self._issue(client.client_id, authorization_code.scopes)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        if not self.refresh.pop(refresh_token.token, None):  # rotated: the old one dies
            raise TokenError("invalid_grant", "refresh token was already used")
        return self._issue(client.client_id, scopes or refresh_token.scopes)

    async def load_access_token(self, token: str) -> AccessToken | None:
        found = self.access.get(token)
        return found if found and found.expires_at > time.time() else None

    async def revoke_token(self, token) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    def _issue(self, client_id: str, scopes: list[str]) -> OAuthToken:
        now = int(time.time())
        self.access = {k: v for k, v in self.access.items() if v.expires_at > now}
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.access[access] = AccessToken(token=access, client_id=client_id, scopes=scopes,
                                          expires_at=now + ACCESS_TTL, resource=self.resource, subject="owner")
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes,
                                             expires_at=now + REFRESH_TTL, resource=self.resource, subject="owner")
        return OAuthToken(access_token=access, refresh_token=refresh, expires_in=ACCESS_TTL, scope=" ".join(scopes))

    # -- the password page ----------------------------------------------------

    def _page(self, body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width, initial-scale=1'>"
            "<title>UCP sign in</title><body style='font-family:system-ui,sans-serif;max-width:26rem;margin:3rem auto;"
            f"padding:0 1rem'><h2>Connect to UCP</h2>{body}</body>", status_code=status, headers=HEADERS)

    def _form(self, txn: str, error: str = "", status: int = 200) -> HTMLResponse:
        client_id, params, _ = self.pending[txn]
        name = self.clients[client_id].client_name or "An app" if client_id in self.clients else "An app"
        host = urlparse(str(params.redirect_uri)).netloc
        note = f"<p role=alert style='color:#8f4f1f'><b>{html.escape(error)}</b></p>" if error else ""
        return self._page(
            f"<p><b>{html.escape(name)}</b> wants to shop as your UCP agent. Access goes to "
            f"<b>{html.escape(host)}</b>. Continue only if you started this yourself.</p>{note}"
            f"<form method=post action=/login><input type=hidden name=txn value='{html.escape(txn)}'>"
            f"<label>Owner password<br><input type=password name=password autocomplete=current-password required></label>"
            f"<p><button>Allow access</button></p></form>", status)

    async def login(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()} \
            if request.method == "POST" else {}
        txn = form.get("txn") or request.query_params.get("txn", "")
        entry = self.pending.get(txn)
        if not entry or entry[2] <= time.time():
            return self._page("<p>This sign-in has expired. Start again from your app.</p>", 400)
        if request.method == "GET":
            return self._form(txn)
        now = time.time()
        self.failures = [t for t in self.failures if t > now - LOCKOUT]
        # shortcut: one global lockout, so a stranger can lock the owner out for a while; fine for a short demo
        if len(self.failures) >= MAX_FAILURES:
            return self._form(txn, "Too many wrong passwords. Wait 10 minutes.", 429)
        if not hmac.compare_digest(form.get("password", "").encode(), self._password.encode()):
            self.failures.append(now)
            return self._form(txn, "Wrong password.", 401)
        client_id, params, _ = self.pending.pop(txn)
        code = secrets.token_urlsafe(32)
        self.codes[code] = AuthorizationCode(
            code=code, client_id=client_id, scopes=params.scopes or [SCOPE], expires_at=now + CODE_TTL,
            code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=self.resource,
            subject="owner")
        return RedirectResponse(construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state),
                                status_code=302, headers=HEADERS)
