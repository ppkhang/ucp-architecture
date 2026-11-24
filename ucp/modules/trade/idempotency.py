"""Idempotency-Key for POST /v1/intent (hard rule 4, ADR-010)."""
import hashlib
import json
from collections.abc import Callable

from ucp.common.db import connect
from ucp.common.errors import ErrorCode, UCPError


def run(agent_id: str, key: str, body: dict, fn: Callable[[], dict]) -> dict:
    """Run fn once per (agent, key) for 24 hours: replay the stored result, refuse the same key with another body.

    Only successes are stored. A failed call frees the key so the agent can retry it, and the retry is safe
    because order placing is idempotent at the platform through the forwarded key."""
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with connect() as conn:
        # forget a key after 24 h, and a request that never finished (crash) after 2 minutes
        conn.execute("DELETE FROM trade.idempotency WHERE agent_id = %s AND key = %s AND (created_at < now() - "
                     "interval '24 hours' OR (response IS NULL AND created_at < now() - interval '2 minutes'))",
                     (agent_id, key))
        mine = conn.execute("INSERT INTO trade.idempotency (agent_id, key, request_hash) VALUES (%s, %s, %s) "
                            "ON CONFLICT DO NOTHING RETURNING 1", (agent_id, key, digest)).fetchone()
        old = None if mine else conn.execute(
            "SELECT request_hash, response FROM trade.idempotency WHERE agent_id = %s AND key = %s",
            (agent_id, key)).fetchone()
    if old:
        if old["request_hash"] != digest:
            raise UCPError(ErrorCode.IDEMPOTENCY_CONFLICT, "this Idempotency-Key was used with a different request")
        if old["response"] is None:
            raise UCPError(ErrorCode.IDEMPOTENCY_CONFLICT, "a request with this Idempotency-Key is still running")
        return old["response"]
    try:
        result = fn()
    except BaseException:
        with connect() as conn:
            conn.execute("DELETE FROM trade.idempotency WHERE agent_id = %s AND key = %s", (agent_id, key))
        raise
    with connect() as conn:
        conn.execute("UPDATE trade.idempotency SET response = %s::jsonb WHERE agent_id = %s AND key = %s",
                     (json.dumps(result), agent_id, key))
    return result
