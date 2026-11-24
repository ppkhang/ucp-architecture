"""Shared fixtures: UCP and the mock shop wired together, with a real Postgres."""
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from mock_platform.main import create_app as create_shop
from mock_platform.store import Config
from ucp.api.main import app
from ucp.common.config import get_settings
from ucp.common.db import connect


@pytest.fixture
def ucp():
    shop = TestClient(create_shop(Config(webhook_secret=get_settings().mock_webhook_secret)))
    app.state.platform_http = shop
    with TestClient(app) as c:  # runs lifespan: applies schemas
        with connect() as conn:
            conn.execute("TRUNCATE trade.outbox, trade.platform_events, trade.idempotency, trade.orders, trade.confirm_states, "
                         "trade.checkouts, identity.platform_tokens, identity.user_links, identity.agents")
        c.shop = shop
        c.deliveries = []  # webhooks the shop sent, kept so tests can replay or tamper with them

        def deliver(event, headers, body):
            c.deliveries.append((headers, body))
            c.post("/v1/platform-events/mock", content=body, headers=headers)

        shop.app.state.platform.sender = deliver
        yield c


def agent(c) -> tuple[str, str, dict]:
    r = c.post("/v1/agents", json={"name": "bot"})
    assert r.status_code == 201
    a = r.json()
    t = c.post("/v1/auth/token", json={"grant_type": "api_key"}, headers={"X-API-Key": a["api_key"]})
    assert t.status_code == 200
    return a["agent_id"], a["api_key"], {"Authorization": f"Bearer {t.json()['token']}"}


def link(c, agent_id, auth, user="khang", password="khang123") -> tuple[str, dict]:
    r = c.post(f"/v1/agents/{agent_id}/links", json={"platform_id": "mock"}, headers=auth)
    assert r.status_code == 201
    q = parse_qs(urlparse(r.json()["auth_url"]).query)
    login = c.shop.post("/oauth/authorize", data={"client_id": q["client_id"][0], "redirect_uri": q["redirect_uri"][0],
                                                  "state": q["state"][0], "username": user, "password": password},
                        follow_redirects=False)
    back = parse_qs(urlparse(login.headers["location"]).query)
    cb = c.get("/oauth/callback", params={"code": back["code"][0], "state": back["state"][0]})
    assert cb.status_code == 200
    return r.json()["link_id"], back


def perms(c, agent_id, auth):
    return c.get(f"/v1/agents/{agent_id}/permissions", headers=auth).json()["permissions"]
