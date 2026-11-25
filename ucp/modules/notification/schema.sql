CREATE SCHEMA IF NOT EXISTS notification;

-- The log is the truth, SSE is best effort (addendum C5). seq counts up per agent.
CREATE TABLE IF NOT EXISTS notification.events (
    event_id    uuid PRIMARY KEY, -- the outbox event id: redelivery is deduped on it (hard rule 9)
    agent_id    uuid NOT NULL,
    seq         bigint NOT NULL,
    type        text NOT NULL,
    payload     jsonb NOT NULL,
    occurred_at timestamptz NOT NULL,
    UNIQUE (agent_id, seq)
);
