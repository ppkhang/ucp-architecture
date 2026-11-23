"""Step 4: identity against a real Postgres and the mock shop's OAuth."""
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient

from mock_platform.main import create_app as create_shop
from mock_platform.store import Config
from ucp.api.main import app
from ucp.common.config import get_settings
from ucp.common.db import connect


@pytest.fixture
def ucp():
    app.state.platform_http = TestClient(create_shop(Config(webhook_secret="s")))
    with TestClient(app) as c:  # runs lifespan: applies schemas
        with connect() as conn:
            conn.execute("TRUNCATE identity.platform_tokens, identity.user_links, identity.agents")
        yield c


def agent(c) -> tuple[str, str, dict]:
    r = c.post("/v1/agents", json={"name": "bot"})
    assert r.status_code == 201
    a = r.json()
    t = c.post("/v1/auth/token", json={"grant_type": "api_key"}, headers={"X-API-Key": a["api_key"]})
    assert t.status_code == 200
    return a["agent_id"], a["api_key"], {"Authorization": f"Bearer {t.json()['token']}"}


def link(c, agent_id, auth) -> str:
    r = c.post(f"/v1/agents/{agent_id}/links", json={"platform_id": "mock"}, headers=auth)
    assert r.status_code == 201
    q = parse_qs(urlparse(r.json()["auth_url"]).query)
    shop = app.state.platform_http
    login = shop.post("/oauth/authorize", data={"client_id": q["client_id"][0], "redirect_uri": q["redirect_uri"][0],
                                                "state": q["state"][0], "username": "khang", "password": "khang123"},
                      follow_redirects=False)
    back = parse_qs(urlparse(login.headers["location"]).query)
    cb = c.get("/oauth/callback", params={"code": back["code"][0], "state": back["state"][0]})
    assert cb.status_code == 200
    return r.json()["link_id"], back


def perms(c, agent_id, auth):
    return c.get(f"/v1/agents/{agent_id}/permissions", headers=auth).json()["permissions"]


def test_api_key_gives_read_catalog_only(ucp):
    agent_id, _, auth = agent(ucp)
    assert perms(ucp, agent_id, auth) == ["read_catalog"]


def test_api_key_is_hashed(ucp):
    _, key, _ = agent(ucp)
    with connect() as conn:
        assert conn.execute("SELECT api_key_hash FROM identity.agents").fetchone()["api_key_hash"] != key


def test_bad_api_key(ucp):
    r = ucp.post("/v1/auth/token", json={"grant_type": "api_key"}, headers={"X-API-Key": "nope"})
    assert (r.status_code, r.json()["error"]["code"]) == (401, "UCP_UNAUTHORIZED")


def test_token_lives_five_minutes_and_expires(ucp):
    agent_id, _, _ = agent(ucp)
    old = datetime.now(UTC) - timedelta(seconds=301)
    stale = jwt.encode({"sub": agent_id, "iat": old, "exp": old + timedelta(seconds=300)},
                       get_settings().jwt_secret, algorithm="HS256")
    r = ucp.get(f"/v1/agents/{agent_id}/permissions", headers={"Authorization": f"Bearer {stale}"})
    assert (r.status_code, r.json()["error"]["code"]) == (401, "UCP_SESSION_EXPIRED")


def test_forged_token_refused(ucp):
    agent_id, _, _ = agent(ucp)
    forged = jwt.encode({"sub": agent_id, "exp": datetime.now(UTC) + timedelta(minutes=5)}, "x" * 32, algorithm="HS256")
    r = ucp.get(f"/v1/agents/{agent_id}/permissions", headers={"Authorization": f"Bearer {forged}"})
    assert r.json()["error"]["code"] == "UCP_UNAUTHORIZED"


def test_other_agents_path_forbidden(ucp):
    _, _, auth = agent(ucp)
    other, _, _ = agent(ucp)
    r = ucp.get(f"/v1/agents/{other}/permissions", headers=auth)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "UCP_FORBIDDEN")


def test_suspended_agent_refused_on_next_call(ucp):
    agent_id, _, auth = agent(ucp)
    with connect() as conn:
        conn.execute("UPDATE identity.agents SET status = 'suspended' WHERE id = %s", (agent_id,))
    r = ucp.get(f"/v1/agents/{agent_id}/permissions", headers=auth)
    assert (r.status_code, r.json()["error"]["code"]) == (403, "UCP_AGENT_SUSPENDED")


def test_link_grants_order_scopes_and_encrypts_tokens(ucp):
    agent_id, _, auth = agent(ucp)
    link(ucp, agent_id, auth)
    assert perms(ucp, agent_id, auth) == ["read_catalog", "view_order", "create_order", "cancel_order"]
    with connect() as conn:
        row = conn.execute("SELECT l.platform_user, t.access_token FROM identity.user_links l "
                           "JOIN identity.platform_tokens t ON t.link_id = l.id").fetchone()
    assert row["platform_user"] == "khang"
    shop = app.state.platform_http.app.state.platform
    assert not any(t.encode() in bytes(row["access_token"]) for t in shop.tokens)


def test_callback_state_is_single_use(ucp):
    agent_id, _, auth = agent(ucp)
    _, back = link(ucp, agent_id, auth)
    r = ucp.get("/oauth/callback", params={"code": back["code"][0], "state": back["state"][0]})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "UCP_BAD_REQUEST")


def test_unlink_removes_scopes_and_tokens(ucp):
    agent_id, _, auth = agent(ucp)
    link_id, _ = link(ucp, agent_id, auth)
    assert ucp.delete(f"/v1/agents/{agent_id}/links/{link_id}", headers=auth).status_code == 204
    assert perms(ucp, agent_id, auth) == ["read_catalog"]
    with connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM identity.platform_tokens").fetchone()["n"] == 0
    assert ucp.delete(f"/v1/agents/{agent_id}/links/{link_id}", headers=auth).status_code == 404


def test_unknown_platform_and_bad_body(ucp):
    agent_id, _, auth = agent(ucp)
    r = ucp.post(f"/v1/agents/{agent_id}/links", json={"platform_id": "shopee"}, headers=auth)
    assert r.json()["error"]["code"] == "UCP_NOT_FOUND"
    r = ucp.post("/v1/agents", json={})
    assert (r.status_code, r.json()["error"]["code"]) == (400, "UCP_BAD_REQUEST")
