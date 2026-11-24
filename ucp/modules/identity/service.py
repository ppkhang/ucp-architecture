"""Identity context: agents, API keys, agent JWTs, user links, platform tokens."""
import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet

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
    link_id, state = uuid.uuid4(), secrets.token_urlsafe(24)
    with connect() as conn:
        conn.execute("INSERT INTO identity.user_links (id, agent_id, platform_id, status, state, expires_at) "
                     "VALUES (%s, %s, %s, 'pending', %s, now() + make_interval(secs => %s))",
                     (link_id, agent_id, platform_id, state, LINK_TTL))
    query = urlencode({"client_id": s.mock_client_id, "redirect_uri": f"{s.public_url}/oauth/callback",
                       "state": state})
    return {"link_id": str(link_id), "auth_url": f"{s.mock_base_url}/oauth/authorize?{query}", "expires_in": LINK_TTL}


def finish_link(state: str, code: str, http: httpx.Client) -> str:
    """OAuth callback: swap the code for platform tokens, store them encrypted, activate the link."""
    s = get_settings()
    with connect() as conn:
        # state is single use: clear it as we read it
        link = conn.execute("UPDATE identity.user_links SET state = NULL WHERE state = %s AND status = 'pending' "
                            "AND expires_at > now() RETURNING id", (state,)).fetchone()
    if not link:
        raise UCPError(ErrorCode.BAD_REQUEST, "unknown or expired link request")
    try:
        r = http.post(f"{s.mock_base_url}/oauth/token", data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": f"{s.public_url}/oauth/callback",
            "client_id": s.mock_client_id, "client_secret": s.mock_client_secret})
        if r.status_code != 200:
            raise UCPError(ErrorCode.BAD_REQUEST, "platform refused the authorization code")
        tokens = r.json()
        me = http.get(f"{s.mock_base_url}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        me.raise_for_status()
    except httpx.HTTPError:
        raise UCPError(ErrorCode.PLATFORM_UNAVAILABLE, "platform did not answer") from None
    f = Fernet(s.token_key)
    with connect() as conn:
        conn.execute("UPDATE identity.user_links SET status = 'active', platform_user = %s WHERE id = %s",
                     (me.json()["id"], link["id"]))
        # shortcut: no refresh/rotation yet (hard rule 12, step 7); LINK_EXPIRED comes with it
        conn.execute("INSERT INTO identity.platform_tokens VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
                     (link["id"], f.encrypt(tokens["access_token"].encode()),
                      f.encrypt(tokens["refresh_token"].encode()), tokens["expires_in"]))
    return str(link["id"])


def unlink(agent_id: str, link_id: str) -> None:
    with connect() as conn:
        row = conn.execute("UPDATE identity.user_links SET status = 'revoked', state = NULL "
                           "WHERE id = %s AND agent_id = %s AND status <> 'revoked' RETURNING id",
                           (link_id, agent_id)).fetchone()
        if not row:
            raise UCPError(ErrorCode.NOT_FOUND, "link not found")
        conn.execute("DELETE FROM identity.platform_tokens WHERE link_id = %s", (link_id,))
