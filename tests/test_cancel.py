"""Step 9: cancel_order (UC-03), the checkout intents, BR-26, and refused reconciliation."""
import re
from urllib.parse import parse_qsl, urlparse

import httpx
import pytest

from tests.conftest import agent, link
from tests.test_reliability import rows
from tests.test_trade import (FORM, MUG, checkout, code, confirm_order, intent, pay, setup, user_confirms,
                              user_logs_in)
from ucp.adapters.base import PlatformTimeout
from ucp.adapters.mock.client import MockPlatformAdapter
from ucp.common.config import get_settings
from ucp.common.db import connect
from ucp.modules.trade import workers


def shop(c):
    return c.shop.app.state.platform


def available(c):
    return {v.id: v.available for v in shop(c).variants.values()}


def placed(c, auth, paid=True, key="x"):
    co = checkout(c, auth, f"co-{key}")
    user_confirms(c, co["checkout_id"])
    order = confirm_order(c, auth, co["checkout_id"], f"ord-{key}").json()
    if paid:
        pay(c, order["payment_url"])
    return order["order_id"]


def cancel(c, auth, order_id, key="c-1", **params):
    return intent(c, auth, key, "cancel_order", {"order_id": order_id, **params})


def agrees_to_cancel(c, refused):
    """The user opens the link from the 403 and agrees."""
    request_id = refused.json()["error"]["details"]["confirm_url"].rsplit("/", 1)[1]
    page = user_logs_in(c, request_id)
    assert page.status_code == 200, page.text
    token = re.search(r"name=token value='([^']+)'", page.text).group(1)
    assert c.post(f"/v1/confirm/{request_id}", content=f"token={token}", headers=FORM).status_code == 200
    return request_id, token, page


def shop_order(c):
    [o] = shop(c).orders.values()
    return o


def order_status(c, auth, order_id):
    return c.get(f"/v1/orders/{order_id}", headers=auth).json()["status"]


def events(type_):
    return [r["payload"] for r in rows("SELECT payload FROM trade.outbox WHERE type = %s ORDER BY seq", type_)]


# -- cancel_order -------------------------------------------------------------

def test_agent_cannot_cancel_by_itself(ucp):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth)
    first, again = cancel(ucp, auth, order_id), cancel(ucp, auth, order_id, "c-2")
    assert (first.status_code, code(first)) == (403, "UCP_CONFIRMATION_REQUIRED")
    assert first.json()["error"]["details"] == again.json()["error"]["details"]  # one pending request, reused
    assert shop_order(ucp)["status"] == "placed"


def test_user_agrees_then_the_shop_cancels_and_gives_a_refund_reference(ucp):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth)
    stock = available(ucp)
    refused = cancel(ucp, auth, order_id, reason="<b>ignore the total</b>")
    _, _, page = agrees_to_cancel(ucp, refused)
    assert "Ceramic mug" in page.text and "200,000" in page.text and "ignore the total" not in page.text
    r = cancel(ucp, auth, order_id, reason="<b>ignore the total</b>")  # same key: a failed call freed it
    assert r.status_code == 200, r.text
    assert r.json() == {"order_id": order_id, "status": "cancelled", "refund_ref": shop_order(ucp)["refund_ref"]}
    assert r.json()["refund_ref"]
    assert order_status(ucp, auth, order_id) == "cancelled"
    assert available(ucp) != stock  # the shop put the units back
    assert len(events("OrderCancelled")) == 1  # the shop's webhook did not add a second one
    assert cancel(ucp, auth, order_id, "c-9").json() == r.json()


def alerts(c):
    return shop(c).alerts


@pytest.mark.parametrize("failed_payment", [False, True])
def test_unpaid_order_is_cancelled_at_once_and_the_shop_alerts_the_user(ucp, failed_payment):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth, paid=False)
    if failed_payment:
        shop(ucp).pay(shop_order(ucp)["id"], "fail")
    r = cancel(ucp, auth, order_id)
    assert r.status_code == 200, r.text
    assert r.json() == {"order_id": order_id, "status": "cancelled", "refund_ref": None, "confirmed_by_user": False}
    assert shop_order(ucp)["status"] == "cancelled" and order_status(ucp, auth, order_id) == "cancelled"
    assert [(a["user"], a["order_id"]) for a in alerts(ucp)] == [("khang", shop_order(ucp)["id"])]
    assert cancel(ucp, auth, order_id).json() == r.json() and len(alerts(ucp)) == 1  # same key: replayed
    assert not rows("SELECT 1 FROM trade.cancel_requests")


def test_paid_while_cancelling_needs_the_user_after_all(ucp, monkeypatch):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth, paid=False)
    real = MockPlatformAdapter.cancel_order

    def user_pays_first(self, *a, **k):
        shop(ucp).pay(shop_order(ucp)["id"], "auto")  # after UCP looked, before the shop gets the cancel
        return real(self, *a, **k)

    monkeypatch.setattr(MockPlatformAdapter, "cancel_order", user_pays_first)
    r = cancel(ucp, auth, order_id)
    assert code(r) == "UCP_CONFIRMATION_REQUIRED" and r.json()["error"]["details"]["confirm_url"]
    assert shop_order(ucp)["status"] == "placed" and not alerts(ucp) and not shop(ucp).refunds


def test_the_cached_status_does_not_decide_whether_to_ask(ucp):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth, paid=False)
    shop(ucp).sender = lambda *a: None  # the "paid" webhook gets lost
    pay(ucp, f"http://localhost:9000/pay/{shop_order(ucp)['id']}")
    assert order_status(ucp, auth, order_id) == "pending_payment"
    assert code(cancel(ucp, auth, order_id)) == "UCP_CONFIRMATION_REQUIRED"
    assert shop_order(ucp)["status"] == "placed"


def test_unlinked_agent_cannot_cancel_even_an_unpaid_order(ucp):
    agent_id, _, auth = agent(ucp)
    link_id, _ = link(ucp, agent_id, auth)
    order_id = placed(ucp, auth, paid=False)
    ucp.delete(f"/v1/agents/{agent_id}/links/{link_id}", headers=auth)
    assert cancel(ucp, auth, order_id).status_code == 403
    assert shop_order(ucp)["status"] == "pending_payment"


@pytest.mark.parametrize("reached_the_shop", [True, False])
def test_lost_answer_while_cancelling_unpaid(ucp, monkeypatch, reached_the_shop):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth, paid=False)
    shop(ucp).sender = lambda *a: None
    real = MockPlatformAdapter.cancel_order

    def lost(self, *a, **k):
        if reached_the_shop:
            real(self, *a, **k)
        raise PlatformTimeout()

    with monkeypatch.context() as m:
        m.setattr(MockPlatformAdapter, "cancel_order", lost)
        r = cancel(ucp, auth, order_id)
    if reached_the_shop:
        assert r.json()["confirmed_by_user"] is False
    else:
        assert code(r) == "UCP_PLATFORM_UNAVAILABLE"
        assert cancel(ucp, auth, order_id).status_code == 200
    assert shop_order(ucp)["status"] == "cancelled" and len(alerts(ucp)) == 1


def test_shop_already_shipped_so_it_refuses_even_though_the_cache_is_stale(ucp):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth)
    shop(ucp).sender = lambda *a: None  # the status webhooks get lost
    for status in ("processing", "shipped"):
        ucp.shop.post(f"/_admin/orders/{shop_order(ucp)['id']}/status", params={"status": status})
    assert order_status(ucp, auth, order_id) == "placed"
    agrees_to_cancel(ucp, cancel(ucp, auth, order_id))
    r = cancel(ucp, auth, order_id)
    assert (r.status_code, code(r)) == (409, "UCP_CANNOT_CANCEL")
    assert shop_order(ucp)["status"] == "shipped"
    # that confirmation is used up
    assert code(cancel(ucp, auth, order_id, "c-2")) == "UCP_CONFIRMATION_REQUIRED"


def test_a_purchase_confirmation_cannot_confirm_a_cancel(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    page = user_logs_in(ucp, co["checkout_id"])
    buy_token = re.search(r"name=token value='([^']+)'", page.text).group(1)
    ucp.post(f"/v1/confirm/{co['checkout_id']}", content=f"token={buy_token}", headers=FORM)
    order = confirm_order(ucp, auth, co["checkout_id"]).json()
    pay(ucp, order["payment_url"])  # only a paid order asks the user
    order_id = order["order_id"]
    request_id = cancel(ucp, auth, order_id).json()["error"]["details"]["confirm_url"].rsplit("/", 1)[1]
    r = ucp.post(f"/v1/confirm/{request_id}", content=f"token={buy_token}", headers=FORM)
    assert (r.status_code, code(r)) == (403, "UCP_FORBIDDEN")
    assert code(cancel(ucp, auth, order_id, "c-2")) == "UCP_CONFIRMATION_REQUIRED"


def test_only_the_linked_shop_user_can_agree_and_only_the_owner_can_cancel(ucp):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth)
    request_id = cancel(ucp, auth, order_id).json()["error"]["details"]["confirm_url"].rsplit("/", 1)[1]
    assert code(user_logs_in(ucp, request_id, "linh", "linh123")) == "UCP_FORBIDDEN"
    _, other = setup(ucp, "linh", "linh123")
    assert code(cancel(ucp, other, order_id)) == "UCP_NOT_FOUND"


def test_unlink_refuses_the_cancel(ucp):
    agent_id, _, auth = agent(ucp)
    link_id, _ = link(ucp, agent_id, auth)
    order_id = placed(ucp, auth)
    agrees_to_cancel(ucp, cancel(ucp, auth, order_id))
    ucp.delete(f"/v1/agents/{agent_id}/links/{link_id}", headers=auth)
    assert cancel(ucp, auth, order_id).status_code == 403
    assert shop_order(ucp)["status"] == "placed"


@pytest.mark.parametrize("reached_the_shop", [True, False])
def test_lost_cancel_answer_never_cancels_twice(ucp, monkeypatch, reached_the_shop):
    _, auth = setup(ucp)
    order_id = placed(ucp, auth)
    agrees_to_cancel(ucp, cancel(ucp, auth, order_id))
    real, keys = MockPlatformAdapter.cancel_order, []

    def lost(self, token, shop_id, key):
        keys.append(key)
        if reached_the_shop:
            real(self, token, shop_id, key)
        raise PlatformTimeout()

    monkeypatch.setattr(MockPlatformAdapter, "cancel_order", lost)
    r = cancel(ucp, auth, order_id)
    if reached_the_shop:
        assert r.status_code == 200 and r.json()["refund_ref"]  # looked it up, no second call
    else:
        assert code(r) == "UCP_PLATFORM_UNAVAILABLE"
        monkeypatch.setattr(MockPlatformAdapter, "cancel_order",
                            lambda self, t, i, k: keys.append(k) or real(self, t, i, k))
        assert cancel(ucp, auth, order_id, "c-2").status_code == 200  # no new confirmation needed
        assert keys[0] == keys[1]  # resent with the first key
    assert shop_order(ucp)["status"] == "cancelled" and len(shop(ucp).refunds) == 1


# -- checkout intents ---------------------------------------------------------

def change(c, auth, checkout_id, method, key, **params):
    return intent(c, auth, key, method, {"checkout_id": checkout_id, **params})


def test_cancelled_checkout_releases_stock_and_cannot_be_confirmed_or_ordered(ucp):
    _, auth = setup(ucp)
    before = available(ucp)
    co = checkout(ucp, auth)
    page = user_logs_in(ucp, co["checkout_id"])
    token = re.search(r"name=token value='([^']+)'", page.text).group(1)
    r = change(ucp, auth, co["checkout_id"], "cancel_checkout", "x-1")
    assert r.json() == {"checkout_id": co["checkout_id"], "status": "cancelled"}
    assert available(ucp) == before and events("CheckoutCancelled")[0]["reason"] == "agent"
    assert ucp.get(f"/v1/confirm/{co['checkout_id']}", follow_redirects=False).status_code == 410
    late = ucp.post(f"/v1/confirm/{co['checkout_id']}", content=f"token={token}", headers=FORM)
    assert late.status_code == 410
    assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_CHECKOUT_EXPIRED"
    assert not shop(ucp).orders


def test_confirmed_checkout_that_is_then_cancelled_is_not_ordered(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    change(ucp, auth, co["checkout_id"], "cancel_checkout", "x-1")
    assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_CHECKOUT_EXPIRED"
    assert not shop(ucp).orders


def test_shipping_and_promotion_requote_and_drop_the_confirmation(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)  # 180,000 + 20,000
    held = available(ucp)
    user_confirms(ucp, co["checkout_id"])
    r = change(ucp, auth, co["checkout_id"], "select_shipping_method", "s-1", shipping_method_id="express")
    assert r.status_code == 200, r.text
    assert (r.json()["proposal"]["shipping_fee"], r.json()["proposal"]["total"]) == (50_000, 230_000)
    assert r.json()["expires_at"] <= co["expires_at"]  # changing is not a free extension
    assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_CONFIRMATION_REQUIRED"
    r = change(ucp, auth, co["checkout_id"], "apply_promotion", "p-1", code="SAVE20K")
    p = r.json()["proposal"]
    assert (p["shipping_method_id"], p["discount"], p["total"]) == ("express", 20_000, 210_000)
    assert p["items"][0]["name"] == co["proposal"]["items"][0]["name"]
    assert available(ucp) == held  # one hold, not three
    page = user_confirms(ucp, co["checkout_id"])
    assert "express" in page.text and "discount 20,000" in page.text and "210,000" in page.text
    order = confirm_order(ucp, auth, co["checkout_id"], "ord-2").json()
    assert shop_order(ucp)["total"] == 210_000 and order["status"] == "pending_payment"
    assert events("ShippingMethodSelected")[0]["shipping_method_id"] == "express"


def test_bad_shipping_method_or_code_leaves_the_checkout_as_it_was(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    for key, method, params in (("b-1", "select_shipping_method", {"shipping_method_id": "drone"}),
                                ("b-2", "apply_promotion", {"code": "NOPE"}),
                                ("b-3", "apply_promotion", {"code": "SAVE100K"})):
        assert code(change(ucp, auth, co["checkout_id"], method, key, **params)) == "UCP_BAD_REQUEST"
    assert confirm_order(ucp, auth, co["checkout_id"]).status_code == 200


def test_failed_requote_after_the_hold_is_released_closes_the_checkout(ucp, monkeypatch):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])

    def down(*a, **k):
        raise PlatformTimeout()

    monkeypatch.setattr(MockPlatformAdapter, "create_checkout", down)
    r = change(ucp, auth, co["checkout_id"], "apply_promotion", "p-1", code="SAVE20K")
    assert code(r) == "UCP_PLATFORM_UNAVAILABLE"
    assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_CHECKOUT_EXPIRED"
    assert events("CheckoutCancelled")[0]["reason"] == "change_failed"


def test_a_checkout_being_changed_cannot_be_ordered_and_a_stuck_one_expires(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET status = 'updating'")
    assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_BAD_REQUEST"
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET expires_at = now() - interval '1 second'")
    assert workers.expire_checkouts(ucp.shop) == 1


def test_extend_works_once_and_never_past_the_hold(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    ucp.shop.post("/_admin/clock", params={"advance_seconds": 120})
    r = change(ucp, auth, co["checkout_id"], "extend_checkout", "e-1")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["expires_at"] > co["expires_at"] and got["expires_at"] <= got["hold_expires_at"]
    assert got["proposal"] == co["proposal"]
    assert code(change(ucp, auth, co["checkout_id"], "extend_checkout", "e-2")) == "UCP_BAD_REQUEST"
    assert rows("SELECT status FROM trade.checkouts")[0]["status"] == "open"
    assert len(events("CheckoutExtended")) == 1


def test_another_agent_cannot_change_a_checkout(ucp):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    _, other = setup(ucp, "linh", "linh123")
    assert code(change(ucp, other, co["checkout_id"], "cancel_checkout", "x-1")) == "UCP_NOT_FOUND"


def test_shipping_options(ucp):
    _, auth = setup(ucp)
    r = ucp.get("/v1/shipping/options", params={"platform_id": "mock", "city": "HN"}, headers=auth)
    assert [(o["method_id"], o["fee"]) for o in r.json()["options"]] == [("standard", 25_000), ("express", 55_000)]
    assert ucp.get("/v1/shipping/options", params={"platform_id": "mock", "city": "HN"}).status_code == 401


# -- BR-26 --------------------------------------------------------------------

ONE_MUG = [{"product_id": "p_mug", "qty": 1}]


def open_checkout(c, auth, key):
    return intent(c, auth, key, "create_checkout", {"platform_id": "mock", "items": ONE_MUG})


def test_limit_counts_every_agent_of_the_same_shop_user(ucp):
    _, a = setup(ucp)
    _, b = setup(ucp)
    first = open_checkout(ucp, a, "1").json()
    assert open_checkout(ucp, a, "2").status_code == 200 and open_checkout(ucp, b, "3").status_code == 200
    held = available(ucp)
    r = open_checkout(ucp, b, "4")
    assert (r.status_code, code(r)) == (429, "UCP_QUOTA_EXCEEDED")
    assert available(ucp) == held  # refused before the shop held anything
    _, linh = setup(ucp, "linh", "linh123")
    assert open_checkout(ucp, linh, "5").status_code == 200  # the limit is per shop user
    change(ucp, a, first["checkout_id"], "cancel_checkout", "x-1")
    assert open_checkout(ucp, b, "6").status_code == 200


def test_limit_is_rechecked_after_the_shop_answers(ucp, monkeypatch):
    _, auth = setup(ucp)
    before = available(ucp)
    real = MockPlatformAdapter.create_checkout

    def slots_taken_meanwhile(self, *a, **k):
        made = real(self, *a, **k)
        monkeypatch.setattr(get_settings(), "max_open_checkouts", 0)
        return made

    monkeypatch.setattr(MockPlatformAdapter, "create_checkout", slots_taken_meanwhile)
    assert code(open_checkout(ucp, auth, "1")) == "UCP_QUOTA_EXCEEDED"
    assert available(ucp) == before and not rows("SELECT 1 FROM trade.checkouts")  # its hold was released


def test_three_expiries_in_a_row_start_a_cooldown(ucp):
    _, auth = setup(ucp)
    for key in ("1", "2", "3"):
        assert open_checkout(ucp, auth, key).status_code == 200
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET expires_at = now() - interval '1 minute'")
    r = open_checkout(ucp, auth, "4")
    assert code(r) == "UCP_QUOTA_EXCEEDED" and r.json()["error"]["details"]["cooldown_seconds"] == 600
    with connect() as conn:
        conn.execute("UPDATE trade.checkouts SET expires_at = now() - interval '11 minutes'")
    assert open_checkout(ucp, auth, "5").status_code == 200


# -- a refused reconciliation (left over from step 7) ---------------------------

def test_refusal_found_by_the_sync_worker_is_told_to_the_agent(ucp, monkeypatch):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])

    def never_arrived(*a, **k):
        raise PlatformTimeout()

    with monkeypatch.context() as m:
        m.setattr(MockPlatformAdapter, "place_order", never_arrived)
        assert code(confirm_order(ucp, auth, co["checkout_id"])) == "UCP_PLATFORM_UNAVAILABLE"
    for c in shop(ucp).checkouts.values():
        c.status = "expired"
    workers.sync_orders(ucp.shop, min_age=0)
    assert not rows("SELECT 1 FROM trade.orders")
    assert events("CheckoutCancelled")[0]["reason"] == "UCP_CHECKOUT_EXPIRED"
    for key in ("ord-1", "ord-2"):
        assert code(confirm_order(ucp, auth, co["checkout_id"], key)) == "UCP_CHECKOUT_EXPIRED"


def test_out_of_stock_refusal_keeps_its_code_on_retry(ucp):
    shop(ucp).config.supports_holds = False
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    user_confirms(ucp, co["checkout_id"])
    ucp.shop.post("/_admin/variants/v_mug", params={"stock": 0})
    for key in ("ord-1", "ord-2"):
        assert code(confirm_order(ucp, auth, co["checkout_id"], key)) == "UCP_INSUFFICIENT_STOCK"


@pytest.mark.parametrize("tail", [")", ").", "**"])
def test_confirm_and_payment_links_survive_stray_punctuation(ucp, tail):
    _, auth = setup(ucp)
    co = checkout(ucp, auth)
    assert ucp.get(f"/v1/confirm/{co['checkout_id']}{tail}", follow_redirects=False).status_code == 302
    user_confirms(ucp, co["checkout_id"])
    order = confirm_order(ucp, auth, co["checkout_id"]).json()
    path = f"/pay/{shop_order(ucp)['id']}{tail}"
    assert "Pay your order" in ucp.shop.get(path).text
    pay(ucp, order["payment_url"])
    request_id = cancel(ucp, auth, order["order_id"]).json()["error"]["details"]["confirm_url"].rsplit("/", 1)[1]
    assert ucp.get(f"/v1/confirm/{request_id}{tail}", follow_redirects=False).status_code == 302
    assert ucp.get("/v1/confirm/not-an-id)", follow_redirects=False).status_code == 400


def on_shop(url):
    """Same host as the test sign-in, so the test client sends the shop cookie."""
    return urlparse(url)._replace(scheme="", netloc="").geturl()


def test_a_signed_in_browser_goes_straight_to_agree_but_cannot_link_that_way(ucp):
    agent_id, auth = setup(ucp)
    first, second = checkout(ucp, auth, "co-1"), checkout(ucp, auth, "co-2")
    user_confirms(ucp, first["checkout_id"])  # signs in at the shop, which remembers the browser
    to_shop = ucp.get(f"/v1/confirm/{second['checkout_id']}", follow_redirects=False).headers["location"]
    back = ucp.shop.get(on_shop(to_shop), follow_redirects=False)
    assert back.status_code == 302  # no Continue click
    page = ucp.get("/oauth/callback", params=dict(parse_qsl(urlparse(back.headers["location"]).query)))
    assert page.status_code == 200 and "Agree and confirm" in page.text
    assert code(confirm_order(ucp, auth, second["checkout_id"])) == "UCP_CONFIRMATION_REQUIRED"  # still needs Agree

    # a stranger rewrites a linking address to the sign-in-only client: the silent code cannot link
    other, _, other_auth = agent(ucp)
    url = ucp.post(f"/v1/agents/{other}/links", json={"platform_id": "mock"}, headers=other_auth).json()["auth_url"]
    silent = ucp.shop.get(on_shop(url.replace("client_id=ucp&", "client_id=ucp-confirm&")), follow_redirects=False)
    assert silent.status_code == 302
    r = ucp.get("/oauth/callback", params=dict(parse_qsl(urlparse(silent.headers["location"]).query)))
    assert (r.status_code, code(r)) == (400, "UCP_BAD_REQUEST")
    assert ucp.get(f"/v1/agents/{other}/permissions", headers=other_auth).json()["permissions"] == ["read_catalog"]

