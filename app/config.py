from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "TicketDhaka"
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/ticketdhaka"
    redis_url: str | None = None  # optional: falls back to the in-process LRU cache

    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 7

    hold_minutes: int = 5
    expiry_sweep_seconds: int = 15

    rate_limit_capacity: int = 60      # burst size per client
    rate_limit_refill_per_sec: float = 10.0
    rate_limit_enabled: bool = True

    cache_enabled: bool = True
    cache_capacity: int = 256
    cache_ttl_seconds: int = 30

    seed_demo_data: bool = True

    # The owner's admin account, created or promoted at startup. Set these as secrets, never commit them.
    admin_email: str | None = None
    admin_password: str | None = None

    # aamarPay (Bangladesh: bKash, Nagad, Rocket, cards). Leave empty to use the built-in demo gateway.
    # Free sandbox credentials from aamarPay's docs: store_id aamarpaytest,
    # signature_key dbb74894e82415a2f7ff0ec3a97e4183. No real money moves in the sandbox.
    aamarpay_store_id: str | None = None
    aamarpay_signature_key: str | None = None
    aamarpay_sandbox: bool = True  # False only with live credentials from aamarPay


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # Render/Heroku style URLs -> SQLAlchemy psycopg3 driver
    if s.database_url.startswith("postgres://"):
        s.database_url = s.database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif s.database_url.startswith("postgresql://"):
        s.database_url = s.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    return s
