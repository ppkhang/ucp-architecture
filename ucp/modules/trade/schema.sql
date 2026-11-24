CREATE SCHEMA IF NOT EXISTS trade;

-- No foreign keys into identity: agent_id and link_id are plain ids (one context, one schema).
CREATE TABLE IF NOT EXISTS trade.checkouts (
    id                   uuid PRIMARY KEY,
    agent_id             uuid NOT NULL,
    link_id              uuid NOT NULL,
    platform_id          text NOT NULL,
    platform_user        text NOT NULL,
    platform_checkout_id text NOT NULL,
    items                jsonb NOT NULL,
    region               text NOT NULL,
    proposal             jsonb NOT NULL,
    proposal_hash        text NOT NULL,
    expires_at           timestamptz NOT NULL,
    hold_expires_at      timestamptz,
    -- the user's confirmation, bound to the hash, the user and the agent (hard rule 3)
    confirmed_at         timestamptz,
    confirmed_hash       text,
    confirmed_user       text,
    consumed_at          timestamptz,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS trade.confirm_states (
    state       text PRIMARY KEY,
    checkout_id uuid NOT NULL REFERENCES trade.checkouts(id),
    expires_at  timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS trade.orders (
    id                uuid PRIMARY KEY,
    checkout_id       uuid NOT NULL UNIQUE REFERENCES trade.checkouts(id),
    agent_id          uuid NOT NULL,
    platform_id       text NOT NULL,
    link_id           uuid NOT NULL,
    idempotency_key   text NOT NULL UNIQUE, -- the key forwarded to the platform, namespaced by agent
    platform_order_id text,
    status            text NOT NULL DEFAULT 'pending_payment',
    payment_status    text NOT NULL DEFAULT 'pending',
    reconciling       boolean NOT NULL DEFAULT true,
    total             bigint,
    payment_url       text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS orders_platform_order
    ON trade.orders (platform_id, platform_order_id) WHERE platform_order_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS trade.idempotency (
    agent_id     uuid NOT NULL,
    key          text NOT NULL,
    request_hash text NOT NULL,
    response     jsonb, -- NULL while the first request is still running
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, key)
);

CREATE TABLE IF NOT EXISTS trade.platform_events (
    platform_id text NOT NULL,
    event_id    text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (platform_id, event_id)
);
