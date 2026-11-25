"""Identity context: agents, API keys, agent JWTs, user links, platform tokens."""
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet, InvalidToken

from ucp.adapters import PLATFORMS
from ucp.common.config import get_settings
from ucp.common.db import connect
from ucp.common.errors import ErrorCode, UCPError

TOKEN_TTL = 300  # hard rule 11: JWT lives 5 minutes
LINK_TTL = 600
LINKED_SCOPES = ["view_order", "create_order", "cancel_order"]


def _hash(api_key: str) -> str:
    # API keys are 256-bit random, so a plain SHA-256 is enough (no brute force to slow down)
    return hashlib.sha256(api_key.encode()).hexdigest()


def register_agent(name: str, description: str | None) -> dict:
    agent_id, api_key = uuid.uuid4(), "ucp_" + secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute("INSERT INTO identity.agents (id, name, description, api_key_hash) VALUES (%s, %s, %s, %s)",
                     (agent_id, name, description, _hash(api_key)))
    return {"agent_id": str(agent_id), "api_key": api_key, "tier": "free"}


def permissions(agent_id: str) -> list[str]:
    with connect() as conn:
        linked = conn.execute("SELECT 1 FROM identity.user_links WHERE agent_id = %s AND status = 'active' LIMIT 1",
                              (agent_id,)).fetchone()
    # hard rule 11: API key alone gives read_catalog; order scopes need a UserLink
    return ["read_catalog"] + (LINKED_SCOPES if linked else [])


def issue_token(api_key: str | None) -> dict:
    with connect() as conn:
        agent = conn.execute("SELECT id, status FROM identity.agents WHERE api_key_hash = %s",
                             (_hash(api_key or ""),)).fetchone()
    if not agent:
        raise UCPError(ErrorCode.UNAUTHORIZED, "invalid API key")
    if agent["status"] != "active":
        raise UCPError(ErrorCode.AGENT_SUSPENDED, "agent is suspended")
    now = datetime.now(UTC)
    token = jwt.encode({"sub": str(agent["id"]), "iat": now, "exp": now + timedelta(seconds=TOKEN_TTL)},
                       get_settings().jwt_secret, algorithm="HS256")
    return {"token": token, "expires_in": TOKEN_TTL, "permissions": permissions(str(agent["id"]))}


def authenticate(authorization: str | None) -> str:
    """Bearer JWT to agent_id. The agent row is checked on every call (hard rule 11)."""
    if not authorization or not authorization.startswith("Bearer "):
        raise UCPError(ErrorCode.UNAUTHORIZED, "missing bearer token")
    try:
        claims = jwt.decode(authorization[7:], get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise UCPError(ErrorCode.SESSION_EXPIRED, "token expired") from None
    except jwt.InvalidTokenError:
        raise UCPError(ErrorCode.UNAUTHORIZED, "invalid token") from None
    if "purpose" in claims:  # confirmation-page tokens share the key but are never agent sessions
        raise UCPError(ErrorCode.UNAUTHORIZED, "invalid token")
    # shortcut: session check reads Postgres each call instead of a Redis session; move to Redis with the gateway
    with connect() as conn:
        agent = conn.execute("SELECT status FROM identity.agents WHERE id = %s", (claims["sub"],)).fetchone()
    if not agent:
        raise UCPError(ErrorCode.UNAUTHORIZED, "agent not found")
    if agent["status"] != "active":
        raise UCPError(ErrorCode.AGENT_SUSPENDED, "agent is suspended")
    return claims["sub"]


def start_link(agent_id: str, platform_id: str) -> dict:
    if platform_id not in PLATFORMS:  # shared registry with the catalog
        raise UCPError(ErrorCode.NOT_FOUND, f"unknown platform {platform_id}")
    s = get_settings()
    link_id, state = uuid.uuid4(), "l_" + secrets.token_urlsafe(24)  # "c_" states belong to the trade confirmation page
    with connect() as conn:
        conn.execute("INSERT INTO identity.user_links (id, agent_id, platform_id, status, state, expires_at) "
                     "VALUES (%s, %s, %s, 'pending', %s, now() + make_interval(secs => %s))",
                     (link_id, agent_id, platform_id, state, LINK_TTL))
    query = urlencode({"client_id": s.mock_client_id, "redirect_uri": f"{s.public_url}/oauth/callback",
                       "state": state})
    return {"link_id": str(link_id), "auth_url": f"{s.mock_base_url}/oauth/authorize?{query}", "expires_in": LINK_TTL}


def platform_login(code: str, http: httpx.Client, client_id: str | None = None) -> tuple[str, dict]:
    """Swap an OAuth code for the platform user id and tokens. The caller decides what to keep."""
    s = get_settings()
    try:
        r = http.post(f"{s.mock_base_url}/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": f"{s.public_url}/oauth/callback",
            "client_id": client_id or s.mock_client_id, "client_secret": s.mock_client_secret})
        if r.status_code != 200:
            raise UCPError(ErrorCode.BAD_REQUEST, "platform refused the authorization code")
        tokens = r.json()
        me = http.get(f"{s.mock_base_url}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        me.raise_for_status()
    except httpx.HTTPError:
        raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, "platform did not answer") from None
    return me.json()["id"], tokens


def finish_link(state: str, code: str, http: httpx.Client) -> str:
    """OAuth callback: swap the code for platform tokens, store them encrypted, activate the link."""
    with connect() as conn:
        # state is single use: clear it as we read it
        link = conn.execute("UPDATE identity.user_links SET state = NULL WHERE state = %s AND status = 'pending' "
                            "AND expires_at > now() RETURNING id", (state,)).fetchone()
    if not link:
        raise UCPError(ErrorCode.BAD_REQUEST, "unknown or expired link request")
    platform_user, tokens = platform_login(code, http)
    f = Fernet(get_settings().token_key)
    with connect() as conn:
        conn.execute("UPDATE identity.user_links SET status = 'active', platform_user = %s WHERE id = %s",
                     (platform_user, link["id"]))
        conn.execute("INSERT INTO identity.platform_tokens VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
                     (link["id"], f.encrypt(tokens["access_token"].encode()),
                      f.encrypt(tokens["refresh_token"].encode()), tokens["expires_in"]))
    return str(link["id"])


def _decrypt(token: bytes) -> str:
    """Stored platform tokens unreadable with the current TOKEN_KEY (key changed or unset after a restart) mean the
    user must link again. The link is not marked expired: fixing the key brings every link back."""
    try:
        return Fernet(get_settings().token_key).decrypt(bytes(token)).decode()
    except InvalidToken:
        raise UCPError(ErrorCode.LINK_EXPIRED, "the stored platform token cannot be read, link the account again") from None


def _select_link(agent_id: str, platform_id: str | None, link_id: str | None) -> dict | None:
    with connect() as conn:
        return conn.execute(
            "SELECT l.id, l.status, l.platform_user, t.access_token, t.expires_at <= now() AS token_expired "
            "FROM identity.user_links l JOIN identity.platform_tokens t ON t.link_id = l.id "
            "WHERE l.agent_id = %s AND l.status IN ('active', 'expired') "
            "AND (l.platform_id = %s OR %s::text IS NULL) AND (l.id = %s OR %s::uuid IS NULL) "
            "ORDER BY l.created_at DESC LIMIT 1", (agent_id, platform_id, platform_id, link_id, link_id)).fetchone()


def _refresh(link_id: str, http: httpx.Client) -> None:
    """Swap the refresh token for new tokens. The shop rotates the refresh token, so both are replaced (rule 12).

    shortcut: the token row stays locked across the platform call so two requests cannot spend the same refresh
    token; the call has a short timeout. Move to a queue if refreshes get slow."""
    s, f, dead = get_settings(), Fernet(get_settings().token_key), False
    with connect() as conn:
        row = conn.execute("SELECT refresh_token, expires_at <= now() AS expired FROM identity.platform_tokens "
                           "WHERE link_id = %s FOR UPDATE", (link_id,)).fetchone()
        if not row or not row["expired"]:
            return  # gone, or another request refreshed while we waited for the lock
        refresh_token = _decrypt(row["refresh_token"])
        try:
            r = http.post(f"{s.mock_base_url}/oauth/token", timeout=5, data={
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": s.mock_client_id, "client_secret": s.mock_client_secret})
        except httpx.HTTPError:
            raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, "platform did not answer") from None
        try:
            reason = r.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            reason = None
        if r.status_code == 200:
            t = r.json()
            conn.execute("UPDATE identity.platform_tokens SET access_token = %s, refresh_token = %s, "
                         "expires_at = now() + make_interval(secs => %s) WHERE link_id = %s",
                         (f.encrypt(t["access_token"].encode()), f.encrypt(t["refresh_token"].encode()),
                          t["expires_in"], link_id))
        elif reason == "INVALID_GRANT":  # only a dead refresh token means the user must link again
            conn.execute("UPDATE identity.user_links SET status = 'expired' WHERE id = %s", (link_id,))
            dead = True
        elif reason == "INVALID_CLIENT":  # our own credentials are wrong: a config error, never the user's link
            raise UCPError(ErrorCode.INTERNAL_ERROR, "the platform rejected UCP's client credentials")
        else:
            raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, f"platform answered {r.status_code}")
    if dead:
        raise UCPError(ErrorCode.LINK_EXPIRED, "the platform no longer accepts this link, link the account again")


def get_link(agent_id: str, platform_id: str | None = None, link_id: str | None = None,
             http: httpx.Client | None = None) -> dict:
    """The agent's newest link (on the platform, or the given one) with a usable decrypted platform token.

    This is the create_order check: no link means UCP_NO_USER_LINK. Trade calls it again at confirm_order, so an
    unlink in between is refused. An access token past its expiry is refreshed first when `http` is given."""
    row = _select_link(agent_id, platform_id, link_id)
    if not row:
        raise UCPError(ErrorCode.NO_USER_LINK, "link a user account first")
    if row["status"] == "expired":
        raise UCPError(ErrorCode.LINK_EXPIRED, "the platform no longer accepts this link, link the account again")
    if row["token_expired"]:
        if http is None:
            raise UCPError(ErrorCode.LINK_EXPIRED, "platform token expired")
        _refresh(str(row["id"]), http)
        row = _select_link(agent_id, platform_id, str(row["id"]))
        if not row or row["token_expired"]:
            raise UCPError(ErrorCode.LINK_EXPIRED, "platform token expired")
    return {"link_id": str(row["id"]), "platform_user": row["platform_user"],
            "access_token": _decrypt(row["access_token"])}


def unlink(agent_id: str, link_id: str) -> None:
    with connect() as conn:
        row = conn.execute("UPDATE identity.user_links SET status = 'revoked', state = NULL "
                           "WHERE id = %s AND agent_id = %s AND status <> 'revoked' RETURNING id",
                           (link_id, agent_id)).fetchone()
        if not row:
            raise UCPError(ErrorCode.NOT_FOUND, "link not found")
        conn.execute("DELETE FROM identity.platform_tokens WHERE link_id = %s", (link_id,))
