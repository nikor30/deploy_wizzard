"""Application settings loaded from PNPB_-prefixed environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PNPB_", env_file=".env", extra="ignore")

    # Fernet key for encrypting stored credentials. Optional until P1 wires the
    # credential store; P1 must make startup fail fast when it is missing.
    secret_key: str | None = None
    db_path: str = "/data/pnpb.sqlite"
    log_level: str = "INFO"
    port: int = 8060


@lru_cache
def get_settings() -> Settings:
    return Settings()
