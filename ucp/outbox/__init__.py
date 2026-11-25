"""Transactional outbox (hard rule 9). `emit` runs inside the caller's transaction; `relay` hands rows to in-process
consumers. RabbitMQ only comes once there is a second consumer (AGENTS.md section 4)."""
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

from ucp.common.db import connect

log = logging.getLogger("ucp.outbox")
SCHEMAS = ("trade",)  # one outbox per context that emits events; table names below come from this list only
HANDLERS: list[Callable[[dict[str, Any]], None]] = []  # consumers register here. They must dedupe on event["id"].


def emit(conn: Any, schema: str, type_: str, agent_id: Any, payload: dict[str, Any]) -> None:
    """Payloads carry ids and statuses only, never tokens (hard rule 13)."""
    assert schema in SCHEMAS
    conn.execute(f"INSERT INTO {schema}.outbox (id, type, agent_id, payload) VALUES (%s, %s, %s, %s::jsonb)",
                 (uuid.uuid4(), type_, agent_id, json.dumps(payload)))


def relay(batch: int = 50) -> int:
    """Deliver pending events in order. A failing consumer leaves the event pending and counts an attempt.

    One relay per schema at a time, so a second instance cannot deliver later events ahead of earlier ones.

    shortcut: handlers run inside the claiming transaction, so they must be fast and local; a poison event blocks
    its queue (the only consumer is a local insert), add a dead-letter after N attempts when one can fail for good."""
    if not HANDLERS:
        return 0  # nobody is listening yet: keep the events so a consumer added later still gets them
    delivered = 0
    for schema in SCHEMAS:
        with connect() as conn:
            if not conn.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok",
                                (f"outbox.{schema}",)).fetchone()["ok"]:
                continue  # another instance is relaying
            rows = conn.execute(f"SELECT id, type, agent_id, payload, created_at FROM {schema}.outbox "
                                "WHERE delivered_at IS NULL ORDER BY seq LIMIT %s FOR UPDATE SKIP LOCKED",
                                (batch,)).fetchall()
            for r in rows:
                event = {"id": str(r["id"]), "type": r["type"], "agent_id": str(r["agent_id"]),
                         "payload": r["payload"], "created_at": r["created_at"]}
                try:
                    for handler in HANDLERS:
                        handler(event)
                except Exception:
                    log.exception("outbox consumer failed for %s", r["id"])
                    conn.execute(f"UPDATE {schema}.outbox SET attempts = attempts + 1 WHERE id = %s", (r["id"],))
                    break  # keep the order: later events wait behind this one
                conn.execute(f"UPDATE {schema}.outbox SET delivered_at = now() WHERE id = %s", (r["id"],))
                delivered += 1
    return delivered
