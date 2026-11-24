"""Step 7: outbox, ExpiryWorker, OrderSyncWorker, token refresh, idempotency of stuck requests."""
import httpx
import pytest
from cryptography.fernet import Fernet

from tests.conftest import agent, link
from tests.test_trade import checkout, code, confirm_order, intent, pay, setup, user_confirms
from ucp import outbox
from ucp.api.main import app
from ucp.common.config import get_settings
from ucp.common.db import connect
from ucp.modules.trade import workers
from ucp.workers.loop import run_once


def rows(sql, *args):
    with connect() as conn:
        return conn.execute(sql, args).fetchall()


def placed_order(ucp, auth):
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    order = confirm_order(ucp, auth, co["checkout_id"]).json()
    return co, order


def test_status_change_and_event_are_written_together(ucp):
    _, auth = setup(ucp)
    _, order = placed_order(ucp, auth)
    pay(ucp, order["payment_url"])
    ev = rows("SELECT type, payload, delivered_at FROM trade.outbox")
    assert [e["type"] for e in ev] == ["OrderPlaced"] and ev[0]["delivered_at"] is None
    assert ev[0]["payload"]["order_id"] == order["order_id"] and ev[0]["payload"]["status"] == "placed"
    assert "token" not in str(ev[0]["payload"]).lower()  # ids and statuses only (rule 13)


def test_relay_keeps_events_until_a_consumer_exists_then_delivers_in_order(ucp, monkeypatch):
    _, auth = setup(ucp)
    _, order = placed_order(ucp, auth)
    pay(ucp, order["payment_url"])
    ucp.shop.post("/_admin/orders/" + ucp.shop.app.state.platform.orders.__iter__().__next__() + "/status",
                  params={"status": "processing"})  # a second event
    assert outbox.relay() == 0  # no consumer yet: nothing is marked delivered
    seen = []
    monkeypatch.setattr(outbox, "HANDLERS", [lambda e: seen.append((e["type"], e["id"]))])
    assert outbox.relay() == 2
    assert [t for t, _ in seen] == ["OrderPlaced", "OrderStatusChanged"] and len({i for _, i in seen}) == 2
    assert outbox.relay() == 0  # delivered once


def test_broker_down_expiry_still_runs_and_events_wait(ucp, monkeypatch):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET expires_at = now() - interval '1 second'")

    def boom(_):
        raise RuntimeError("consumer down")

    monkeypatch.setattr(outbox, "HANDLERS", [boom])
    result = run_once(app.state.platform_http)
    assert result["expired"] == 1 and result["relayed"] == 0
    ev = rows("SELECT type, attempts, delivered_at FROM trade.outbox")
    assert [(e["type"], e["attempts"], e["delivered_at"]) for e in ev] == [("CheckoutExpired", 1, None)]
    assert rows("SELECT status FROM trade.checkouts")[0]["status"] == "expired"
    seen = []
    monkeypatch.setattr(outbox, "HANDLERS", [lambda e: seen.append(e["type"])])
    assert outbox.relay() == 1 and seen == ["CheckoutExpired"]


def test_expiry_releases_a_live_hold_and_skips_used_checkouts(ucp):
    _, auth = setup(ucp)
    open_co, used = checkout(ucp, auth, "a"), checkout(ucp, auth, "b")
    user_confirms(ucp, used["checkout_id"])
    confirm_order(ucp, auth, used["checkout_id"], "o")
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET expires_at = now() - interval '1 second'")  # hold is still live
    assert workers.expire_checkouts(app.state.platform_http) == 1
    shop = ucp.shop.app.state.platform
    assert [c.status for c in shop.checkouts.values()].count("released") == 1  # the shop's hold was freed
    assert workers.expire_checkouts(app.state.platform_http) == 0  # once only
    assert [r["status"] for r in rows("SELECT status FROM trade.checkouts ORDER BY created_at")] == ["expired", "open"]


def test_lost_webhook_is_caught_by_the_sync_worker(ucp):
    _, auth = setup(ucp)
    _, order = placed_order(ucp, auth)
    ucp.shop.post("/_admin/clock", params={"advance_seconds": 1900})  # payment deadline (1800 s) passes, the shop cancels
    ucp.shop.get("/health")  # any request lets the shop apply its deadlines
    # the shop's webhook is signed with its own (moved) clock, so UCP rejects it as stale: the trap from step 6
    assert ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()["status"] == "pending_payment"
    assert workers.sync_orders(app.state.platform_http, min_age=0) == 1
    got = ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()
    assert got["status"] == "cancelled"
    assert [e["type"] for e in rows("SELECT type FROM trade.outbox")] == ["OrderCancelled"]


def test_sync_walks_several_missed_steps_and_never_goes_back(ucp):
    _, auth = setup(ucp)
    _, order = placed_order(ucp, auth)
    shop = ucp.shop.app.state.platform
    shop.sender = None  # every webhook is lost
    pay(ucp, order["payment_url"])
    plat_id = next(iter(shop.orders))
    for status in ("processing", "shipped"):
        ucp.shop.post(f"/_admin/orders/{plat_id}/status", params={"status": status})
    assert workers.sync_orders(app.state.platform_http, min_age=0) == 1
    assert ucp.get(f"/v1/orders/{order['order_id']}", headers=auth).json()["status"] == "shipped"
    assert [e["payload"]["status"] for e in rows("SELECT payload FROM trade.outbox ORDER BY seq")] == [
        "placed", "processing", "shipped"]
    before = rows("SELECT updated_at FROM trade.orders")[0]["updated_at"]
    assert workers.sync_orders(app.state.platform_http, min_age=0) == 1  # polled again, nothing new
    assert len(rows("SELECT 1 FROM trade.outbox")) == 3
    assert rows("SELECT updated_at FROM trade.orders")[0]["updated_at"] == before  # a poll is not a change


def test_sync_worker_settles_a_reconciling_order_into_one_shop_order(ucp):
    agent_id, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    shop, left = ucp.shop, {"n": 2}

    def flaky(request):  # the shop gets the request but the answer is lost, for placeOrder and for the lookup
        r = shop.request(request.method, str(request.url), content=request.content, headers=request.headers)
        if request.url.path == "/orders" and left["n"]:
            left["n"] -= 1
            raise httpx.ReadTimeout("lost", request=request)
        return httpx.Response(r.status_code, content=r.content, headers={"content-type": "application/json"})

    app.state.platform_http = httpx.Client(transport=httpx.MockTransport(flaky))
    assert code(confirm_order(ucp, auth, co["checkout_id"], "k")) == "UCP_PLATFORM_UNAVAILABLE"
    assert rows("SELECT reconciling FROM trade.orders")[0]["reconciling"] is True
    assert workers.sync_orders(app.state.platform_http, min_age=60) == 0  # too fresh: confirm_order may be in flight
    assert workers.sync_orders(app.state.platform_http, min_age=0) >= 1  # settles, then polls it like any active order
    assert rows("SELECT reconciling FROM trade.orders")[0]["reconciling"] is False
    assert len(shop.app.state.platform.orders) == 1


def test_token_refresh_rotates_both_tokens(ucp):
    agent_id, auth = setup(ucp)
    before = rows("SELECT access_token, refresh_token FROM identity.platform_tokens")[0]
    with connect() as conn:
        conn.execute("UPDATE identity.platform_tokens SET expires_at = now() - interval '1 minute'")
    checkout(ucp, auth)  # the call itself refreshes first
    after = rows("SELECT access_token, refresh_token, expires_at > now() AS fresh FROM identity.platform_tokens")[0]
    assert after["fresh"] and bytes(after["access_token"]) != bytes(before["access_token"])
    assert bytes(after["refresh_token"]) != bytes(before["refresh_token"])


def test_refused_refresh_token_expires_the_link(ucp):
    agent_id, auth = setup(ucp)
    with connect() as conn:
        conn.execute("UPDATE identity.platform_tokens SET expires_at = now() - interval '1 minute'")
    ucp.shop.app.state.platform.refresh_tokens.clear()  # the shop no longer knows the refresh token
    r = intent(ucp, auth, "k", "create_checkout", {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 1}]})
    assert (r.status_code, code(r)) == (403, "UCP_LINK_EXPIRED")
    assert rows("SELECT status FROM identity.user_links")[0]["status"] == "expired"
    r = intent(ucp, auth, "k2", "create_checkout", {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 1}]})
    assert code(r) == "UCP_LINK_EXPIRED"  # still says expired, not "no link"
    link(ucp, agent_id, auth)  # linking again fixes it
    assert intent(ucp, auth, "k3", "create_checkout",
                  {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 1}]}).status_code == 200


@pytest.mark.parametrize("token_expired", [False, True])  # read the access token / the refresh token
def test_unreadable_platform_token_is_link_expired_not_500(ucp, monkeypatch, token_expired):
    agent_id, auth = setup(ucp)
    if token_expired:
        with connect() as conn:
            conn.execute("UPDATE identity.platform_tokens SET expires_at = now() - interval '1 minute'")
    monkeypatch.setattr(get_settings(), "token_key", Fernet.generate_key().decode())  # TOKEN_KEY changed
    r = intent(ucp, auth, "k", "create_checkout", {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 1}]})
    assert (r.status_code, code(r)) == (403, "UCP_LINK_EXPIRED")
    assert rows("SELECT status FROM identity.user_links")[0]["status"] == "active"  # fixing the key brings it back


def test_wrong_client_secret_does_not_kill_the_users_link(ucp, monkeypatch):
    _, auth = setup(ucp)
    with connect() as conn:
        conn.execute("UPDATE identity.platform_tokens SET expires_at = now() - interval '1 minute'")
    monkeypatch.setattr(get_settings(), "mock_client_secret", "wrong")  # our own config error
    r = intent(ucp, auth, "k", "create_checkout", {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 1}]})
    assert code(r) == "UCP_INTERNAL_ERROR"
    assert rows("SELECT status FROM identity.user_links")[0]["status"] == "active"


def test_stuck_idempotency_row_is_retried_after_two_minutes(ucp):
    agent_id, auth = setup(ucp)
    body = {"method": "create_checkout", "params": {"platform_id": "mock", "items": [{"product_id": "p_mug", "qty": 1}]}}
    with connect() as conn:  # a request that crashed before storing its result
        import hashlib, json
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        conn.execute("INSERT INTO trade.idempotency (agent_id, key, request_hash) VALUES (%s, 'stuck', %s)",
                     (agent_id, digest))
    r = ucp.post("/v1/intent", json=body, headers={**auth, "Idempotency-Key": "stuck"})
    assert (r.status_code, code(r)) == (409, "UCP_IDEMPOTENCY_CONFLICT")  # still running as far as anyone knows
    with connect() as conn:
        conn.execute("UPDATE trade.idempotency SET created_at = now() - interval '3 minutes'")
    assert ucp.post("/v1/intent", json=body, headers={**auth, "Idempotency-Key": "stuck"}).status_code == 200


def test_run_once_reports_each_worker(ucp):
    assert run_once(app.state.platform_http) == {"expired": 0, "synced": 0, "relayed": 0}
