CREATE SCHEMA IF NOT EXISTS identity;

CREATE TABLE IF NOT EXISTS identity.agents (
    id           uuid PRIMARY KEY,
    name         text NOT NULL,
    description  text,
    tier         text NOT NULL DEFAULT 'free',
    api_key_hash text NOT NULL UNIQUE,
    status       text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity.user_links (
    id            uuid PRIMARY KEY,
    agent_id      uuid NOT NULL REFERENCES identity.agents(id),
    platform_id   text NOT NULL,
    platform_user text,
    status        text NOT NULL CHECK (status IN ('pending', 'active', 'revoked')),
    state         text UNIQUE,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL
);

-- Kept apart from user_links (hard rule 12). Values are Fernet ciphertext.
CREATE TABLE IF NOT EXISTS identity.platform_tokens (
    link_id       uuid PRIMARY KEY REFERENCES identity.user_links(id) ON DELETE CASCADE,
    access_token  bytea NOT NULL,
    refresh_token bytea NOT NULL,
    expires_at    timestamptz NOT NULL
);
