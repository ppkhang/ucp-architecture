"""Runs the workers on a timer inside the api process. Safe with several instances: every worker claims rows with
SKIP LOCKED."""
import asyncio
import logging

import httpx

from ucp import outbox
from ucp.common.config import get_settings
from ucp.modules.trade import workers as trade

log = logging.getLogger("ucp.workers")


def run_once(http: httpx.Client) -> dict[str, int]:
    """Expiry first: it never depends on the relay or a broker (rule 10)."""
    steps = {"expired": lambda: trade.expire_checkouts(http),
             "synced": lambda: trade.sync_orders(http, get_settings().sync_min_age_seconds),
             "relayed": outbox.relay}
    result = {}
    for name, step in steps.items():
        try:
            result[name] = step()
        except Exception:  # one failing worker must not stop the others
            log.exception("worker %s failed", name)
            result[name] = -1
    return result


async def run_forever(http: httpx.Client, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await asyncio.to_thread(run_once, http)
