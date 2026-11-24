from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://ucp:ucp@127.0.0.1:5432/ucp"
    redis_url: str = "redis://localhost:6379/0"
    # Secrets: dev defaults only, set real values in .env outside local dev.
    jwt_secret: str = "dev-only-jwt-secret-change-me-32bytes"
    token_key: str = "ZGV2LW9ubHktZmVybmV0LWtleS1jaGFuZ2UtbWUhISE="  # Fernet key (urlsafe base64, 32 bytes)
    public_url: str = "http://localhost:8000"
    mock_base_url: str = "http://localhost:9000"
    mock_client_id: str = "ucp"
    mock_client_secret: str = "ucp-mock-secret"
    mock_webhook_secret: str = "mock-webhook-secret"  # same default as the mock shop
    checkout_ttl_seconds: int = 1800  # 30 minutes, v1.0 BR-14 (addendum B6)


@lru_cache
def get_settings() -> Settings:
    return Settings()
