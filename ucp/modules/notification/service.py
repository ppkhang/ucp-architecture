"""Notification context: the per-agent event log, fed by the outbox relay."""
import json
from datetime import UTC
from typing import Any

from ucp.common.db import connect


def record(event: dict[str, Any]) -> None:
    """Outbox consumer. Uses its own connection: no cross-schema work in the producer's transaction (hard rule 8)."""
    with connect() as conn:
        # one writer per agent at a time, so seqs commit in order and a reader's cursor never skips one
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (event["agent_id"],))
        conn.execute(
            "INSERT INTO notification.events (event_id, agent_id, seq, type, payload, occurred_at) "
            "SELECT %s, %s, coalesce(max(seq), 0) + 1, %s, %s::jsonb, %s FROM notification.events WHERE agent_id = %s "
            "ON CONFLICT (event_id) DO NOTHING",
            (event["id"], event["agent_id"], event["type"], json.dumps(event["payload"]), event["created_at"],
             event["agent_id"]))


def events_after(agent_id: str, after: int, limit: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute("SELECT seq, event_id, type, payload, occurred_at FROM notification.events "
                            "WHERE agent_id = %s AND seq > %s ORDER BY seq LIMIT %s", (agent_id, after, limit)).fetchall()
    return [{"seq": r["seq"], "event_id": str(r["event_id"]), "event_type": r["type"], "payload": r["payload"],
             "occurred_at": r["occurred_at"].astimezone(UTC).isoformat().replace("+00:00", "Z")} for r in rows]


def last_seq(agent_id: str) -> int:
    with connect() as conn:
        return conn.execute("SELECT coalesce(max(seq), 0) AS s FROM notification.events WHERE agent_id = %s",
                            (agent_id,)).fetchone()["s"]
