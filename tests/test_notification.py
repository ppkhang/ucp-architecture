"""Step 8: the per-agent event log, replay and the SSE stream."""
import json
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from tests.conftest import agent, link
from tests.test_reliability import placed_order, rows
from tests.test_trade import pay, setup
from ucp import outbox
from ucp.common.config import get_settings
from ucp.common.db import connect
from ucp.modules.notification import service


@pytest.fixture(autouse=True)
def short_streams(monkeypatch):
    # TestClient reads a whole response body, so a stream has to end by itself
    monkeypatch.setattr(get_settings(), "sse_max_seconds", 0.3)
    monkeypatch.setattr(get_settings(), "sse_poll_seconds", 0.05)


def event(agent_id, n) -> dict:
    return {"id": f"00000000-0000-0000-0000-{n:012d}", "type": "OrderStatusChanged", "agent_id": agent_id,
            "payload": {"n": n}, "created_at": datetime.now(UTC)}


def replay(c, auth, **params):
    return c.get("/v1/events", params={"after": 0, **params}, headers=auth)


def stream(c, auth, **headers) -> list[dict]:
    r = c.get("/v1/events/stream", headers={**auth, **headers})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    out = []
    for block in r.text.split("\n\n"):
        if block.startswith("id: "):
            first, data = block.split("\n")
            out.append(json.loads(data.removeprefix("data: ")))
            assert first == f"id: {out[-1]['seq']}"
    return out


def test_paid_order_reaches_the_agent(ucp):
    _, auth = setup(ucp)
    _, order = placed_order(ucp, auth)
    pay(ucp, order["payment_url"])
    assert outbox.relay() == 1
    [e] = replay(ucp, auth).json()["events"]
    assert (e["seq"], e["event_type"], e["payload"]["order_id"]) == (1, "OrderPlaced", order["order_id"])
    assert e["occurred_at"].endswith("Z")
    assert e["event_id"] == str(rows("SELECT id FROM trade.outbox")[0]["id"])


def test_redelivery_is_deduped_and_leaves_no_gap(ucp):
    a, auth = setup(ucp)
    service.record(event(a, 1))
    service.record(event(a, 1))
    service.record(event(a, 2))
    assert [(e["seq"], e["payload"]["n"]) for e in replay(ucp, auth).json()["events"]] == [(1, 1), (2, 2)]


def test_cursor_limit_and_isolation_between_agents(ucp):
    a, auth = setup(ucp)
    b, _, other = agent(ucp)
    link(ucp, b, other)
    for n in (1, 2, 3):
        service.record(event(a, n))
    service.record(event(b, 9))
    assert [e["seq"] for e in replay(ucp, auth, after=1, limit=1).json()["events"]] == [2]
    assert [(e["seq"], e["payload"]["n"]) for e in replay(ucp, other).json()["events"]] == [(1, 9)]
    for bad in ({"after": -1}, {"limit": 0}, {"limit": 501}, {"after": 2**64}):
        assert replay(ucp, auth, **bad).status_code == 400
    assert ucp.get("/v1/events", headers=auth).status_code == 400  # `after` is required


@pytest.mark.parametrize("path", ["/v1/events?after=0", "/v1/events/stream"])
def test_events_need_a_session_and_a_user_link(ucp, path):
    assert ucp.get(path).status_code == 401
    a, _, auth = agent(ucp)
    assert ucp.get(path, headers=auth).json()["error"]["code"] == "UCP_NO_USER_LINK"
    link_id, _ = link(ucp, a, auth)
    assert ucp.delete(f"/v1/agents/{a}/links/{link_id}", headers=auth).status_code == 204
    assert ucp.get(path, headers=auth).status_code == 403


def test_stream_resumes_from_last_event_id_and_new_stream_skips_history(ucp):
    a, auth = setup(ucp)
    for n in (1, 2, 3):
        service.record(event(a, n))
    assert [e["seq"] for e in stream(ucp, auth, **{"Last-Event-ID": "1"})] == [2, 3]
    assert stream(ucp, auth) == []
    assert ucp.get("/v1/events/stream", headers={**auth, "Last-Event-ID": "x"}).status_code == 400


def test_stream_delivers_an_event_recorded_while_it_is_open(ucp, monkeypatch):
    a, auth = setup(ucp)
    real, calls = service.events_after, []

    def later(*args):
        calls.append(1)
        if len(calls) == 2:  # as if written by another instance: only the database is shared
            service.record(event(a, 7))
        if len(calls) == 3:  # ends the stream without relying on timing
            with connect() as conn:
                conn.execute("UPDATE identity.agents SET status = 'suspended' WHERE id = %s", (a,))
        return real(*args)

    monkeypatch.setattr(service, "events_after", later)
    monkeypatch.setattr(get_settings(), "sse_max_seconds", 10)
    assert [e["payload"]["n"] for e in stream(ucp, auth)] == [7]


@pytest.mark.parametrize("change", ["UPDATE identity.agents SET status = 'suspended' WHERE id = %s",
                                    "UPDATE identity.user_links SET status = 'revoked' WHERE agent_id = %s"])
def test_open_stream_ends_when_the_agent_loses_access(ucp, monkeypatch, change):
    a, auth = setup(ucp)
    calls = []

    def then_revoke(*args):
        calls.append(1)
        with connect() as conn:
            conn.execute(change, (a,))
        return []

    monkeypatch.setattr(service, "events_after", then_revoke)
    monkeypatch.setattr(get_settings(), "sse_max_seconds", 5)
    assert stream(ucp, auth) == [] and len(calls) == 1


def test_open_stream_ends_when_the_token_expires(ucp, monkeypatch):
    a, _ = setup(ucp)
    token = jwt.encode({"sub": a, "exp": datetime.now(UTC) + timedelta(seconds=2)}, get_settings().jwt_secret, "HS256")
    monkeypatch.setattr(get_settings(), "sse_max_seconds", 30)
    start = datetime.now(UTC)
    assert stream(ucp, {"Authorization": f"Bearer {token}"}) == []
    assert (datetime.now(UTC) - start).total_seconds() < 10


def test_only_one_relay_runs_at_a_time(ucp):
    a, _ = setup(ucp)
    with connect() as conn:
        outbox.emit(conn, "trade", "CheckoutExpired", a, {})
    with connect() as holder:
        holder.execute("SELECT pg_advisory_xact_lock(hashtext('outbox.trade'))")
        assert outbox.relay() == 0
    assert outbox.relay() == 1
