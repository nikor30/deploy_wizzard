"""Fernet encryption for secrets at rest and masking for API responses."""

from cryptography.fernet import Fernet, InvalidToken

from app.errors import ConfigurationError

MASK_SUFFIX_LEN = 4


class SecretBox:
    def __init__(self, key: str) -> None:
        try:
            self._fernet = Fernet(key.encode())
        except (ValueError, TypeError) as exc:
            raise ConfigurationError(
                "PNPB_SECRET_KEY is not a valid Fernet key. Generate one with: "
                "python -c 'from cryptography.fernet import Fernet; "
                "print(Fernet.generate_key().decode())'"
            ) from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise ConfigurationError(
                "Stored secret cannot be decrypted with the current PNPB_SECRET_KEY. "
                "The key changed since the secret was saved; re-enter the credential."
            ) from exc


def mask_secret(plaintext: str | None) -> str | None:
    """Return the masked representation shown by the API, e.g. '****abcd'."""
    if not plaintext:
        return None
    if len(plaintext) <= MASK_SUFFIX_LEN:
        return "****"
    return "****" + plaintext[-MASK_SUFFIX_LEN:]
