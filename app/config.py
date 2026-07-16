"""Application settings loaded from PNPB_-prefixed environment variables."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigurationError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PNPB_", env_file=".env", extra="ignore")

    # Fernet key for encrypting stored credentials. Required — create_app fails
    # fast when it is missing so a misconfigured container never runs silently.
    secret_key: str | None = None
    db_path: str = "/data/pnpb.sqlite"
    log_level: str = "INFO"
    port: int = 8060

    def require_secret_key(self) -> str:
        if not self.secret_key:
            raise ConfigurationError(
                "PNPB_SECRET_KEY is not set. Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            )
        return self.secret_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
