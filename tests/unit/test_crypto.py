import pytest
from app.crypto import SecretBox, mask_secret
from app.errors import ConfigurationError
from cryptography.fernet import Fernet


def test_encrypt_decrypt_roundtrip() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    assert box.decrypt(box.encrypt("s3cret-token")) == "s3cret-token"


def test_ciphertext_does_not_contain_plaintext() -> None:
    box = SecretBox(Fernet.generate_key().decode())
    assert "s3cret" not in box.encrypt("s3cret-token")


def test_invalid_key_raises_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="Fernet key"):
        SecretBox("not-a-key")


def test_decrypt_with_wrong_key_raises_configuration_error() -> None:
    ciphertext = SecretBox(Fernet.generate_key().decode()).encrypt("x")
    other = SecretBox(Fernet.generate_key().decode())
    with pytest.raises(ConfigurationError, match="re-enter"):
        other.decrypt(ciphertext)


@pytest.mark.parametrize(
    ("plaintext", "expected"),
    [
        ("supersecret1234", "****1234"),
        ("abcd", "****"),
        ("ab", "****"),
        ("", None),
        (None, None),
    ],
)
def test_mask_secret(plaintext: str | None, expected: str | None) -> None:
    assert mask_secret(plaintext) == expected
