"""Шифр пароля подключения: AES-256-GCM, ключ из `PORTAL_SECRET_KEY` (docs/portal-api.md §2.7).

Ключ выводится HKDF-SHA256 с меткой `portal/db-password`; AAD — 16 байт идентификатора
подключения: шифртекст, перенесённый в строку другого подключения, не расшифруется.
"""

import secrets
from uuid import UUID

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_LABEL = b"portal/db-password"
_NONCE_BYTES = 12
_KEY_BYTES = 32


class AesPasswordCipher:
    """Реализация порта `PasswordCipher`."""

    def __init__(self, secret_key: bytes) -> None:
        """Вывести ключ шифрования паролей подключений."""
        key = HKDF(algorithm=hashes.SHA256(), length=_KEY_BYTES, salt=None, info=_LABEL).derive(
            secret_key
        )
        self._aead = AESGCM(key)

    def __repr__(self) -> str:
        """Без ключа."""
        return "AesPasswordCipher()"

    def encrypt(self, connection_id: UUID, password: str) -> bytes:
        """12 байт nonce и шифртекст с меткой подлинности."""
        nonce = secrets.token_bytes(_NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, password.encode(), connection_id.bytes)

    def decrypt(self, connection_id: UUID, data: bytes) -> str:
        """Пароль; другой AAD или подмена поднимает `InvalidTag`."""
        plain = self._aead.decrypt(data[:_NONCE_BYTES], data[_NONCE_BYTES:], connection_id.bytes)
        return plain.decode()
