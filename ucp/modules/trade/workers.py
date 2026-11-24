"""Trade's background work: ExpiryWorker and OrderSyncWorker (ADR-010).

Each worker claims rows with FOR UPDATE SKIP LOCKED and commits before it calls a platform, so two api instances
never take the same row and no transaction stays open across a platform request. Neither needs a broker (rule 10)."""
import logging

import httpx

from ucp import outbox
from ucp.adapters import get_adapter
from ucp.common.db import connect
from ucp.common.errors import UCPError
from ucp.modules.identity import service as identity
from ucp.modules.trade import service

log = logging.getLogger("ucp.trade.workers")
ACTIVE = ("pending_payment", "pre_ordered", "placed", "processing", "shipped")


def expire_checkouts(http: httpx.Client) -> int:
    """ExpiryWorker: expire open checkouts past expires_at and release a hold that is still live."""
    with connect() as conn:
        rows = conn.execute(
            "UPDATE trade.checkouts SET status = 'expired' WHERE id IN ("
            "SELECT id FROM trade.checkouts WHERE status = 'open' AND consumed_at IS NULL AND expires_at <= now() "
            "ORDER BY expires_at LIMIT 100 FOR UPDATE SKIP LOCKED) "
            "RETURNING id, agent_id, link_id, platform_id, platform_checkout_id, "
            "coalesce(hold_expires_at > now(), false) AS hold_live").fetchall()
        for r in rows:
            outbox.emit(conn, "trade", "CheckoutExpired", r["agent_id"],
                        {"checkout_id": str(r["id"]), "platform_id": r["platform_id"]})
    for r in rows:
        if not r["hold_live"]:
            continue  # with expires_at = min(TTL, hold) the hold has normally lapsed on its own
        try:  # best effort: if the link is gone or the shop is down, the hold still lapses by itself
            link = identity.get_link(str(r["agent_id"]), link_id=str(r["link_id"]), http=http)
            get_adapter(r["platform_id"], http).release_hold(link["access_token"], r["platform_checkout_id"])
        except UCPError:
            log.info("hold of checkout %s not released, it will lapse", r["id"])
    return len(rows)


def _claim(sql_where: str, min_age: int) -> list[dict]:
    """Take orders for this worker and mark them touched, so other instances skip them for `min_age` seconds."""
    with connect() as conn:
        return conn.execute(
            "UPDATE trade.orders o SET updated_at = now() FROM trade.checkouts c WHERE o.checkout_id = c.id AND o.id IN ("
            f"SELECT id FROM trade.orders WHERE {sql_where} AND updated_at < now() - make_interval(secs => %s) "
            "ORDER BY updated_at LIMIT 50 FOR UPDATE SKIP LOCKED) "
            "RETURNING o.id, o.agent_id, o.platform_id, o.link_id, o.idempotency_key, o.platform_order_id, "
            "o.status, o.payment_status, o.reconciling, c.platform_checkout_id", (min_age,)).fetchall()


def sync_orders(http: httpx.Client, min_age: int = 30) -> int:
    """OrderSyncWorker: settle orders stuck reconciling, and poll active orders as the fallback for lost webhooks."""
    done = 0
    for o in _claim("reconciling", min_age):
        try:
            link = identity.get_link(str(o["agent_id"]), link_id=str(o["link_id"]), http=http)
            # asks the platform by key first, then places with the same key: never a blind resend (rule 5)
            service._place(get_adapter(o["platform_id"], http), link["access_token"], o,
                           o["platform_checkout_id"], resumed=True)
            done += 1
        except UCPError as e:
            log.info("order %s still reconciling: %s", o["id"], e.code)
    active = ",".join(f"'{s}'" for s in ACTIVE)
    for o in _claim(f"NOT reconciling AND platform_order_id IS NOT NULL AND status IN ({active})", min_age):
        try:
            link = identity.get_link(str(o["agent_id"]), link_id=str(o["link_id"]), http=http)
            plat = get_adapter(o["platform_id"], http).get_order(link["access_token"], o["platform_order_id"])
        except UCPError as e:  # no usable link: webhooks still arrive without a token
            log.info("order %s not polled: %s", o["id"], e.code)
            continue
        with connect() as conn:
            row = conn.execute("SELECT id, agent_id, platform_id, status, payment_status FROM trade.orders "
                               "WHERE id = %s FOR UPDATE", (o["id"],)).fetchone()
            service.apply_status(conn, row, plat["status"], plat["payment_status"])
        done += 1
    return done
