"""Порты модуля входа: хранилища, криптография, единица работы."""

from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from portal.auth.domain import BackupCodes, Session, ThrottleEntry, ThrottleScope, User
from portal.core.pagination import SortOrder
from portal.core.ports import AuditLog


class UserRepository(Protocol):
    """Учётные записи."""

    async def get(self, user_id: UUID, *, lock: bool = False) -> User | None:
        """Пользователь по идентификатору; `lock` — держать строку до конца транзакции."""
        ...

    async def by_login(self, login: str, *, lock: bool = False) -> User | None:
        """Пользователь по логину в нижнем регистре; `lock` — как у `get`."""
        ...

    async def add(self, user: User) -> bool:
        """Создать пользователя; `False` — логин занят."""
        ...

    async def update(self, user: User, *fields: str) -> None:
        """Записать только названные поля: остальные мог изменить параллельный запрос."""
        ...

    async def search(
        self, q: str | None, order: SortOrder, offset: int, limit: int
    ) -> tuple[list[User], int]:
        """Страница пользователей по ФИО с отбором по ФИО и логину и общее число."""
        ...


class SessionRepository(Protocol):
    """Сессии."""

    async def get(
        self, session_id: UUID, *, lock_user: bool = False
    ) -> tuple[Session, User] | None:
        """Сессия с её пользователем; `lock_user` — держать строку пользователя."""
        ...

    async def exists(self, session_id: UUID) -> bool:
        """Есть ли сессия в базе."""
        ...

    async def by_token_hash(self, token_hash: bytes) -> tuple[Session, User] | None:
        """Сессия с её пользователем по хешу значения cookie."""
        ...

    async def add(self, session: Session) -> None:
        """Создать сессию."""
        ...

    async def update(self, session: Session, *fields: str) -> None:
        """Записать только названные поля сессии."""
        ...

    async def delete(self, session_id: UUID) -> None:
        """Удалить сессию."""
        ...

    async def delete_for_user(self, user_id: UUID) -> None:
        """Удалить все сессии пользователя."""
        ...

    async def delete_expired(self, user_id: UUID, now: datetime, idle_since: datetime) -> None:
        """Удалить сессии пользователя с вышедшим жёстким сроком или сроком бездействия."""
        ...


class BackupCodeRepository(Protocol):
    """Резервные коды; хранится только HMAC."""

    async def replace(self, user_id: UUID, code_hmacs: Sequence[bytes]) -> None:
        """Заменить все коды пользователя новыми."""
        ...

    async def use(self, user_id: UUID, code_hmac: bytes, now: datetime) -> bool:
        """Пометить неиспользованный код использованным; `False` — такого нет."""
        ...

    async def counts(self, user_id: UUID) -> BackupCodes:
        """Остаток и общее число кодов."""
        ...

    async def delete_for_user(self, user_id: UUID) -> None:
        """Удалить все коды пользователя."""
        ...


class ThrottleRepository(Protocol):
    """Счётчики неудачных попыток (таблица `auth_throttle`)."""

    async def lock_login(self, key: str) -> None:
        """До конца транзакции не пускать другие попытки входа с этим логином."""
        ...

    async def get(self, scope: ThrottleScope, key: str) -> ThrottleEntry | None:
        """Счётчик по ключу."""
        ...

    async def save(self, entry: ThrottleEntry) -> None:
        """Создать или заменить счётчик."""
        ...

    async def add_ip_failure(self, ip: str, now: datetime, window_start: datetime) -> None:
        """Учесть неудачу с адреса; окно, начатое раньше `window_start`, открывается заново."""
        ...

    async def login_locks(self, keys: Sequence[str], now: datetime) -> dict[str, datetime]:
        """Действующие блокировки логинов: ключ счётчика → время окончания."""
        ...

    async def delete(self, scope: ThrottleScope, key: str) -> None:
        """Удалить счётчик."""
        ...

    async def delete_stale(self, last_failure_before: datetime, now: datetime) -> None:
        """Удалить счётчики без недавних неудач и без действующей блокировки."""
        ...


class AuthUnitOfWork(Protocol):
    """Хранилища одной транзакции."""

    users: UserRepository
    sessions: SessionRepository
    backup_codes: BackupCodeRepository
    throttle: ThrottleRepository
    audit: AuditLog

    async def commit(self) -> None:
        """Зафиксировать сделанное; дальнейшая работа идёт в новой транзакции."""
        ...


class AuthUnitOfWorkFactory(Protocol):
    """Открывает единицу работы: фиксация при выходе без ошибки, иначе откат."""

    def __call__(self) -> AbstractAsyncContextManager[AuthUnitOfWork]:
        """Начать транзакцию."""
        ...


class PasswordHasher(Protocol):
    """Хеширование паролей (Argon2id)."""

    def hash(self, password: str) -> str:
        """Закодированная строка хеша."""
        ...

    def verify(self, password_hash: str, password: str) -> bool:
        """Подходит ли пароль к хешу."""
        ...


class SecretCipher(Protocol):
    """Шифрование ключа TOTP и HMAC резервных кодов ключом `PORTAL_SECRET_KEY`."""

    def encrypt(self, plaintext: bytes) -> bytes:
        """Зашифровать: nonce и шифртекст с меткой подлинности."""
        ...

    def decrypt(self, ciphertext: bytes) -> bytes:
        """Расшифровать."""
        ...

    def backup_code_hmac(self, code: str) -> bytes:
        """HMAC нормализованного резервного кода."""
        ...


class TotpProvider(Protocol):
    """Одноразовые коды по времени и QR-код настройки."""

    def new_secret(self) -> str:
        """Новый ключ в base32 (160 случайных бит)."""
        ...

    def code_at(self, secret: str, step: int) -> str:
        """Код для шага времени."""
        ...

    def provisioning_qr(self, secret: str, login: str) -> tuple[int, str]:
        """Размер QR-кода в модулях и данные `d` для `<path>`."""
        ...
