"""Криптография входа: Argon2id, AES-256-GCM и HMAC (docs/portal-api.md §2.7)."""

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher as Argon2Hasher
from argon2 import Type
from argon2.exceptions import InvalidHashError, VerificationError
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from portal.core.settings import Argon2Settings

_NONCE_BYTES = 12
_KEY_BYTES = 32


class Argon2PasswordHasher:
    """Реализация порта `PasswordHasher` на `argon2-cffi`."""

    def __init__(self, settings: Argon2Settings) -> None:
        """Настроить Argon2id параметрами из конфигурации."""
        self._hasher = Argon2Hasher(
            time_cost=settings.time_cost,
            memory_cost=settings.memory_cost_kib,
            parallelism=settings.parallelism,
            type=Type.ID,
        )

    def hash(self, password: str) -> str:
        """Закодированная строка хеша."""
        return self._hasher.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        """Подходит ли пароль к хешу."""
        try:
            return self._hasher.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False


def _derive(secret_key: bytes, label: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=label).derive(
        secret_key
    )


class HkdfSecretCipher:
    """Реализация порта `SecretCipher`: два ключа, выведенные из `PORTAL_SECRET_KEY`."""

    def __init__(self, secret_key: bytes) -> None:
        """Вывести ключ шифрования TOTP и ключ HMAC резервных кодов."""
        self._aead = AESGCM(_derive(secret_key, b"portal/totp-secret"))
        self._hmac_key = _derive(secret_key, b"portal/backup-code")

    def encrypt(self, plaintext: bytes) -> bytes:
        """Зашифровать: 12 байт nonce и шифртекст с меткой подлинности."""
        nonce = secrets.token_bytes(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext, None)

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Расшифровать; подмена данных поднимает `InvalidTag`."""
        return self._aead.decrypt(ciphertext[:_NONCE_BYTES], ciphertext[_NONCE_BYTES:], None)

    def backup_code_hmac(self, code: str) -> bytes:
        """HMAC-SHA256 нормализованного резервного кода."""
        return hmac.new(self._hmac_key, code.encode(), hashlib.sha256).digest()
