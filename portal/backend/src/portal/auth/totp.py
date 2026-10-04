"""Одноразовые коды (`pyotp`) и QR-код настройки (`segno`), docs/portal-api.md §2.4."""

import pyotp
import segno

from portal.auth.domain import TOTP_PERIOD_SECONDS

_SECRET_BASE32_CHARS = 32  # 160 бит


def qr_path(matrix: tuple[bytearray, ...]) -> str:
    """Данные `d` для `<path>`: прямоугольник на каждую полосу тёмных модулей в строке."""
    parts: list[str] = []
    for y, row in enumerate(matrix):
        x = 0
        while x < len(row):
            if not row[x]:
                x += 1
                continue
            start = x
            while x < len(row) and row[x]:
                x += 1
            width = x - start
            parts.append(f"M{start} {y}h{width}v1h-{width}z")
    return "".join(parts)


class PyotpTotpProvider:
    """Реализация порта `TotpProvider`: SHA-1, 6 цифр, период 30 секунд."""

    def __init__(self, issuer: str) -> None:
        """Запомнить название издателя для приложения-аутентификатора."""
        self._issuer = issuer

    def new_secret(self) -> str:
        """Новый ключ в base32 (160 случайных бит)."""
        return pyotp.random_base32(_SECRET_BASE32_CHARS)

    def code_at(self, secret: str, step: int) -> str:
        """Код для шага времени."""
        return pyotp.TOTP(secret).at(step * TOTP_PERIOD_SECONDS)

    def provisioning_qr(self, secret: str, login: str) -> tuple[int, str]:
        """Размер QR-кода в модулях и данные `d` для `<path>`, без полей."""
        uri = pyotp.TOTP(secret, issuer=self._issuer, name=login).provisioning_uri()
        matrix = segno.make(uri, error="m", micro=False).matrix
        return len(matrix), qr_path(matrix)
