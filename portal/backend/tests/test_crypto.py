"""Криптография входа, QR-код и генерация кодов (docs/portal-api.md §2.4, §2.7)."""

import re

import pyotp
import pytest
import segno
from cryptography.exceptions import InvalidTag

from portal.auth.admin import generate_temporary_password
from portal.auth.crypto import Argon2PasswordHasher, HkdfSecretCipher
from portal.auth.service import BACKUP_CODE_ALPHABET, normalize_backup_code
from portal.auth.totp import PyotpTotpProvider, qr_path
from portal.core.settings import Argon2Settings

KEY = bytes(range(32))


def test_password_hash_is_argon2id_and_verifies() -> None:
    hasher = Argon2PasswordHasher(
        Argon2Settings(time_cost=1, memory_cost_kib=8, parallelism=1, workers=1)
    )
    encoded = hasher.hash("пароль-для-проверки")
    assert encoded.startswith("$argon2id$")
    assert hasher.verify(encoded, "пароль-для-проверки")
    assert not hasher.verify(encoded, "другой-пароль")
    assert not hasher.verify("not-a-hash", "пароль-для-проверки")


def test_cipher_roundtrip_uses_fresh_nonce() -> None:
    cipher = HkdfSecretCipher(KEY)
    first, second = cipher.encrypt(b"JBSWY3DPEHPK3PXP"), cipher.encrypt(b"JBSWY3DPEHPK3PXP")
    assert first != second
    assert len(first) == 12 + 16 + 16
    assert cipher.decrypt(first) == cipher.decrypt(second) == b"JBSWY3DPEHPK3PXP"


def test_cipher_rejects_tampering_and_foreign_key() -> None:
    cipher = HkdfSecretCipher(KEY)
    sealed = bytearray(cipher.encrypt(b"secret"))
    sealed[-1] ^= 1
    with pytest.raises(InvalidTag):
        cipher.decrypt(bytes(sealed))
    with pytest.raises(InvalidTag):
        HkdfSecretCipher(bytes(32)).decrypt(cipher.encrypt(b"secret"))


def test_backup_code_hmac_depends_on_key_and_differs_from_encryption_key() -> None:
    cipher = HkdfSecretCipher(KEY)
    assert cipher.backup_code_hmac("ABCD1234") == cipher.backup_code_hmac("ABCD1234")
    assert cipher.backup_code_hmac("ABCD1234") != cipher.backup_code_hmac("ABCD1235")
    assert cipher.backup_code_hmac("ABCD1234") != HkdfSecretCipher(bytes(32)).backup_code_hmac(
        "ABCD1234"
    )


def test_backup_code_alphabet_and_normalization() -> None:
    assert len(BACKUP_CODE_ALPHABET) == 32
    assert not set("ILOU") & set(BACKUP_CODE_ALPHABET)
    assert normalize_backup_code(" ab12-cd34 ") == "AB12CD34"


def test_totp_parameters_match_authenticator_apps() -> None:
    provider = PyotpTotpProvider("Портал сотрудников")
    secret = provider.new_secret()
    assert re.fullmatch(r"[A-Z2-7]{32}", secret)  # 160 бит в base32
    # Контрольный вектор RFC 6238 (SHA-1, 6 цифр, шаг 30 с): время 59 с → шаг 1.
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert provider.code_at(rfc_secret, 1) == "287082"


def test_qr_encodes_provisioning_uri_as_path() -> None:
    provider = PyotpTotpProvider("Портал сотрудников")
    secret = "JBSWY3DPEHPK3PXP"
    size, path = provider.provisioning_qr(secret, "ivanov")
    uri = pyotp.TOTP(secret, issuer="Портал сотрудников", name="ivanov").provisioning_uri()
    assert "secret=JBSWY3DPEHPK3PXP" in uri and "issuer=" in uri and uri.endswith("%D0%BE%D0%B2")
    matrix = segno.make(uri, error="m", micro=False).matrix
    assert size == len(matrix)
    assert re.fullmatch(r"(M\d+ \d+h\d+v1h-\d+z)+", path)

    drawn = [[0] * size for _ in range(size)]
    for x, y, width in re.findall(r"M(\d+) (\d+)h(\d+)v1", path):
        for column in range(int(x), int(x) + int(width)):
            drawn[int(y)][column] = 1
    assert drawn == [[1 if module else 0 for module in row] for row in matrix]


def test_qr_path_of_small_matrix() -> None:
    matrix = (bytearray([1, 1, 0]), bytearray([0, 0, 0]), bytearray([1, 0, 1]))
    assert qr_path(matrix) == "M0 0h2v1h-2zM0 2h1v1h-1zM2 2h1v1h-1z"


@pytest.mark.parametrize(("min_length", "groups"), [(12, 4), (16, 4), (17, 5), (30, 8)])
def test_temporary_password_shape(min_length: int, groups: int) -> None:
    password = generate_temporary_password(min_length)
    parts = password.split("-")
    assert len(parts) == groups
    assert all(re.fullmatch(r"[a-hj-km-np-z2-9]{4}", part) for part in parts)
    assert generate_temporary_password(min_length) != password
