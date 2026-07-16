"""Application settings loaded from PNPB_-prefixed environment variables."""

import logging
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigurationError

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PNPB_", env_file=".env", extra="ignore")

    # Fernet key for encrypting stored credentials. Optional: when unset, a key
    # is generated at startup and persisted next to the DB (see ensure_secret_key)
    # so the container starts with zero configuration.
    secret_key: str | None = None
    db_path: str = "/data/pnpb.sqlite"
    log_level: str = "INFO"
    port: int = 8060

    @property
    def secret_key_path(self) -> Path:
        return Path(self.db_path).parent / "secret.key"

    def ensure_secret_key(self) -> str:
        """Return the Fernet key, generating + persisting one on first start.

        Precedence: PNPB_SECRET_KEY env var > existing key file > newly
        generated key (written 0600 next to the DB so it lives on the same
        volume as the data it protects).
        """
        if self.secret_key:
            return self.secret_key
        key_file = self.secret_key_path
        if key_file.is_file():
            self.secret_key = key_file.read_text().strip()
        else:
            key_file.parent.mkdir(parents=True, exist_ok=True)
            key = Fernet.generate_key().decode()
            key_file.write_text(key)
            key_file.chmod(0o600)
            self.secret_key = key
            logger.warning(
                "PNPB_SECRET_KEY not set - generated a new secret key at %s. "
                "Back it up (or set PNPB_SECRET_KEY): losing it means stored "
                "credentials cannot be decrypted.",
                key_file,
            )
        return self.secret_key

    def require_secret_key(self) -> str:
        if not self.secret_key:
            raise ConfigurationError(
                "No secret key available - startup key resolution did not run. "
                "Set PNPB_SECRET_KEY or restart the application."
            )
        return self.secret_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
