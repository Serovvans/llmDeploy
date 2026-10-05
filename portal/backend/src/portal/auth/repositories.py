"""Хранилища входа на SQLAlchemy Core и единица работы."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from portal.auth.domain import BackupCodes, Session, ThrottleEntry, ThrottleScope, User
from portal.auth.ports import (
    AuthUnitOfWork,
    BackupCodeRepository,
    SessionRepository,
    ThrottleRepository,
    UserRepository,
)
from portal.auth.tables import auth_throttle, backup_codes, sessions, users
from portal.core.audit import SqlAuditLog
from portal.core.pagination import SortOrder
from portal.core.ports import AuditLog, Clock

_SESSION_COLUMNS = [column.label(f"session_{column.name}") for column in sessions.c]


type _Row = sa.Row[*tuple[Any, ...]]


def _user(row: _Row) -> User:
    return User(**{column.name: getattr(row, column.name) for column in users.c})


def _session(row: _Row) -> Session:
    return Session(**{column.name: getattr(row, f"session_{column.name}") for column in sessions.c})


def _like_pattern(q: str) -> str:
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


class SqlUserRepository:
    """Таблица `users`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def _one(self, condition: sa.ColumnElement[bool], lock: bool) -> User | None:
        query = sa.select(users).where(condition)
        if lock:
            query = query.with_for_update()
        row = (await self._connection.execute(query)).first()
        return _user(row) if row else None

    async def get(self, user_id: UUID, *, lock: bool = False) -> User | None:
        """Пользователь по идентификатору; `lock` — `SELECT … FOR UPDATE`."""
        return await self._one(users.c.id == user_id, lock)

    async def by_login(self, login: str, *, lock: bool = False) -> User | None:
        """Пользователь по логину в нижнем регистре; `lock` — `SELECT … FOR UPDATE`."""
        return await self._one(users.c.login == login, lock)

    async def add(self, user: User) -> bool:
        """Создать пользователя; `False` — логин занят."""
        values = {column.name: getattr(user, column.name) for column in users.c}
        try:
            async with self._connection.begin_nested():
                await self._connection.execute(sa.insert(users).values(**values))
        except IntegrityError:
            return False
        return True

    async def update(self, user: User, *fields: str) -> None:
        """Записать только названные поля: остальные мог изменить параллельный запрос."""
        values = {name: getattr(user, name) for name in fields}
        await self._connection.execute(
            sa.update(users).where(users.c.id == user.id).values(**values)
        )

    async def search(
        self, q: str | None, order: SortOrder, offset: int, limit: int
    ) -> tuple[list[User], int]:
        """Страница пользователей по ФИО с отбором по ФИО и логину и общее число."""
        condition: sa.ColumnElement[bool] = sa.true()
        if q:
            pattern = _like_pattern(q)
            condition = sa.or_(
                users.c.full_name.ilike(pattern, escape="\\"),
                users.c.login.ilike(pattern, escape="\\"),
            )
        total = await self._connection.scalar(
            sa.select(sa.func.count()).select_from(users).where(condition)
        )
        direction = sa.asc if order == "asc" else sa.desc
        rows = await self._connection.execute(
            sa.select(users)
            .where(condition)
            .order_by(direction(sa.func.lower(users.c.full_name)), users.c.id)
            .offset(offset)
            .limit(limit)
        )
        return [_user(row) for row in rows], total or 0


class SqlSessionRepository:
    """Таблица `sessions`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def _one(
        self, condition: sa.ColumnElement[bool], lock_user: bool = False
    ) -> tuple[Session, User] | None:
        query = (
            sa.select(*_SESSION_COLUMNS, users)
            .select_from(sessions.join(users, users.c.id == sessions.c.user_id))
            .where(condition)
        )
        if lock_user:
            query = query.with_for_update(of=users)
        row = (await self._connection.execute(query)).first()
        return (_session(row), _user(row)) if row else None

    async def get(
        self, session_id: UUID, *, lock_user: bool = False
    ) -> tuple[Session, User] | None:
        """Сессия с её пользователем; `lock_user` — `FOR UPDATE` строки пользователя."""
        return await self._one(sessions.c.id == session_id, lock_user)

    async def exists(self, session_id: UUID) -> bool:
        """Есть ли сессия в базе."""
        found = await self._connection.scalar(
            sa.select(sessions.c.id).where(sessions.c.id == session_id)
        )
        return found is not None

    async def by_token_hash(self, token_hash: bytes) -> tuple[Session, User] | None:
        """Сессия с её пользователем по хешу значения cookie."""
        return await self._one(sessions.c.token_hash == token_hash)

    async def add(self, session: Session) -> None:
        """Создать сессию."""
        values = {column.name: getattr(session, column.name) for column in sessions.c}
        await self._connection.execute(sa.insert(sessions).values(**values))

    async def update(self, session: Session, *fields: str) -> None:
        """Записать только названные поля сессии."""
        values = {name: getattr(session, name) for name in fields}
        await self._connection.execute(
            sa.update(sessions).where(sessions.c.id == session.id).values(**values)
        )

    async def delete(self, session_id: UUID) -> None:
        """Удалить сессию."""
        await self._connection.execute(sa.delete(sessions).where(sessions.c.id == session_id))

    async def delete_for_user(self, user_id: UUID) -> None:
        """Удалить все сессии пользователя."""
        await self._connection.execute(sa.delete(sessions).where(sessions.c.user_id == user_id))

    async def delete_expired(self, user_id: UUID, now: datetime, idle_since: datetime) -> None:
        """Удалить сессии пользователя с вышедшим жёстким сроком или сроком бездействия."""
        await self._connection.execute(
            sa.delete(sessions).where(
                sessions.c.user_id == user_id,
                sa.or_(sessions.c.expires_at <= now, sessions.c.last_seen_at < idle_since),
            )
        )


class SqlBackupCodeRepository:
    """Таблица `backup_codes`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def replace(self, user_id: UUID, code_hmacs: Sequence[bytes]) -> None:
        """Заменить все коды пользователя новыми."""
        await self.delete_for_user(user_id)
        await self._connection.execute(
            sa.insert(backup_codes),
            [{"id": uuid4(), "user_id": user_id, "code_hmac": value} for value in code_hmacs],
        )

    async def use(self, user_id: UUID, code_hmac: bytes, now: datetime) -> bool:
        """Пометить неиспользованный код использованным; `False` — такого нет."""
        result = await self._connection.execute(
            sa.update(backup_codes)
            .where(
                backup_codes.c.user_id == user_id,
                backup_codes.c.code_hmac == code_hmac,
                backup_codes.c.used_at.is_(None),
            )
            .values(used_at=now)
        )
        return result.rowcount > 0

    async def counts(self, user_id: UUID) -> BackupCodes:
        """Остаток и общее число кодов."""
        row = (
            await self._connection.execute(
                sa.select(
                    sa.func.count().filter(backup_codes.c.used_at.is_(None)), sa.func.count()
                ).where(backup_codes.c.user_id == user_id)
            )
        ).one()
        return BackupCodes(remaining=row[0], total=row[1])

    async def delete_for_user(self, user_id: UUID) -> None:
        """Удалить все коды пользователя."""
        await self._connection.execute(
            sa.delete(backup_codes).where(backup_codes.c.user_id == user_id)
        )


class SqlThrottleRepository:
    """Таблица `auth_throttle`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def lock_login(self, key: str) -> None:
        """Блокировка уровня транзакции: попытки с одним логином идут по очереди."""
        await self._connection.execute(
            sa.select(sa.func.pg_advisory_xact_lock(sa.func.hashtextextended(key, 0)))
        )

    async def get(self, scope: ThrottleScope, key: str) -> ThrottleEntry | None:
        """Счётчик по ключу."""
        row = (
            await self._connection.execute(
                sa.select(auth_throttle).where(
                    auth_throttle.c.scope == scope, auth_throttle.c.key == key
                )
            )
        ).first()
        return ThrottleEntry(**row._asdict()) if row else None

    async def save(self, entry: ThrottleEntry) -> None:
        """Создать или заменить счётчик."""
        values = {column.name: getattr(entry, column.name) for column in auth_throttle.c}
        statement = postgresql.insert(auth_throttle).values(**values)
        await self._connection.execute(
            statement.on_conflict_do_update(
                index_elements=["scope", "key"],
                set_={
                    name: statement.excluded[name]
                    for name in values
                    if name not in {"scope", "key"}
                },
            )
        )

    async def add_ip_failure(self, ip: str, now: datetime, window_start: datetime) -> None:
        """Учесть неудачу с адреса одним атомарным запросом: адрес общий у многих логинов."""
        statement = postgresql.insert(auth_throttle).values(
            scope="ip", key=ip, failures=1, first_failure_at=now, last_failure_at=now
        )
        window_over = auth_throttle.c.first_failure_at <= window_start
        await self._connection.execute(
            statement.on_conflict_do_update(
                index_elements=["scope", "key"],
                set_={
                    "failures": sa.case((window_over, 1), else_=auth_throttle.c.failures + 1),
                    "first_failure_at": sa.case(
                        (window_over, now), else_=auth_throttle.c.first_failure_at
                    ),
                    "last_failure_at": now,
                },
            )
        )

    async def delete(self, scope: ThrottleScope, key: str) -> None:
        """Удалить счётчик."""
        await self._connection.execute(
            sa.delete(auth_throttle).where(
                auth_throttle.c.scope == scope, auth_throttle.c.key == key
            )
        )

    async def delete_stale(self, last_failure_before: datetime, now: datetime) -> None:
        """Удалить счётчики без недавних неудач и без действующей блокировки.

        Строки, занятые другими транзакциями, пропускаются (`SKIP LOCKED`): ожидание их
        в попутной чистке приводило бы к взаимной блокировке параллельных попыток.
        """
        stale = (
            sa.select(auth_throttle.c.scope, auth_throttle.c.key)
            .where(
                auth_throttle.c.last_failure_at < last_failure_before,
                sa.or_(auth_throttle.c.locked_until.is_(None), auth_throttle.c.locked_until <= now),
            )
            .with_for_update(skip_locked=True)
        )
        await self._connection.execute(
            sa.delete(auth_throttle).where(
                sa.tuple_(auth_throttle.c.scope, auth_throttle.c.key).in_(stale)
            )
        )


class SqlAuthUnitOfWork:
    """Хранилища на одном соединении."""

    def __init__(self, connection: AsyncConnection, clock: Clock) -> None:
        """Собрать хранилища вокруг соединения."""
        self._connection = connection
        self.users: UserRepository = SqlUserRepository(connection)
        self.sessions: SessionRepository = SqlSessionRepository(connection)
        self.backup_codes: BackupCodeRepository = SqlBackupCodeRepository(connection)
        self.throttle: ThrottleRepository = SqlThrottleRepository(connection)
        self.audit: AuditLog = SqlAuditLog(connection, clock)

    async def commit(self) -> None:
        """Зафиксировать сделанное; дальнейшая работа идёт в новой транзакции."""
        await self._connection.commit()


class SqlAuthUnitOfWorkFactory:
    """Открывает единицу работы на соединении из пула."""

    def __init__(self, engine: AsyncEngine, clock: Clock) -> None:
        """Запомнить движок и часы."""
        self._engine = engine
        self._clock = clock

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[AuthUnitOfWork]:
        """Фиксация при выходе без ошибки; при ошибке незафиксированное откатывается."""
        async with self._engine.connect() as connection:
            unit = SqlAuthUnitOfWork(connection, self._clock)
            yield unit
            await connection.commit()
