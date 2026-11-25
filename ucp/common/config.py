import secrets
from functools import lru_cache

from cryptography.fernet import Fernet
from pydantic import Field, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Values published in this repo (old defaults, .env.example): anyone could sign tokens or decrypt platform tokens
PUBLIC_SECRETS = {"", "change-me", "dev-only-jwt-secret-change-me-32bytes", "ZGV2LW9ubHktZmVybmV0LWtleS1jaGFuZ2UtbWUhISE="}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)  # hard rule 13

    database_url: str = "postgresql://ucp:ucp@127.0.0.1:5432/ucp"
    redis_url: str = "redis://localhost:6379/0"
    # Secrets: dev defaults only, set real values in .env outside local dev.
    jwt_secret: str = Field(default_factory=lambda: secrets.token_urlsafe(32))  # unset = random per process (dev only)
    # unset = random per process (dev only): stored platform tokens stop decrypting after a restart
    token_key: str = Field(default_factory=lambda: Fernet.generate_key().decode())
    public_url: str = "http://localhost:8000"
    mock_base_url: str = "http://localhost:9000"
    mock_client_id: str = "ucp"
    mock_client_secret: str = "ucp-mock-secret"
    mock_webhook_secret: str = "mock-webhook-secret"  # same default as the mock shop
    workers_interval_seconds: float = 0  # 0 = background workers off (tests call them directly)
    sse_poll_seconds: float = 1
    sse_max_seconds: float = 300  # an event stream closes after this and the client reconnects
    sync_min_age_seconds: int = 30  # an order is not polled or reconciled sooner than this after its last touch
    checkout_ttl_seconds: int = 1800  # 30 minutes, v1.0 BR-14 (addendum B6)
    # BR-26, per shop user and platform across all agents
    max_open_checkouts: int = 3
    checkout_cooldown_after: int = 3  # this many expiries in a row ...
    checkout_cooldown_seconds: int = 600  # ... block new checkouts this long

    @field_validator("jwt_secret", "token_key")
    @classmethod
    def _secret_not_public(cls, v: str, info: ValidationInfo) -> str:
        if v.strip() in PUBLIC_SECRETS:
            raise ValueError(f"{info.field_name.upper()} is blank or a published value; generate one (see .env.example)")
        if info.field_name == "token_key":
            Fernet(v)  # not a Fernet key: refuse at startup instead of a 500 on the first link
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
