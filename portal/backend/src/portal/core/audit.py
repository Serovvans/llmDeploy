"""Журнал аудита: таблица `audit_log` и реализация порта `AuditLog`."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncConnection

from portal.core.db import metadata
from portal.core.ports import Clock

audit_log = sa.Table(
    "audit_log",
    metadata,
    sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    sa.Column("event", sa.Text, nullable=False),
    sa.Column("actor_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("subject_user_id", sa.Uuid, sa.ForeignKey("users.id", ondelete="SET NULL")),
    sa.Column("ip", postgresql.INET),
    sa.Column("details", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'")),
)


class SqlAuditLog:
    """Пишет в `audit_log` в транзакции переданного соединения."""

    def __init__(self, connection: AsyncConnection, clock: Clock) -> None:
        """Привязать журнал к соединению сценария."""
        self._connection = connection
        self._clock = clock

    async def record(
        self,
        event: str,
        *,
        actor_id: UUID | None = None,
        subject_user_id: UUID | None = None,
        ip: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Добавить запись о событии."""
        await self._connection.execute(
            sa.insert(audit_log).values(
                created_at=self._clock.now(),
                event=event,
                actor_id=actor_id,
                subject_user_id=subject_user_id,
                ip=ip,
                details=dict(details or {}),
            )
        )


async def read_audit(
    connection: AsyncConnection, *, since: datetime | None, event: str | None, limit: int
) -> list[sa.Row[*tuple[Any, ...]]]:
    """Последние `limit` записей журнала по возрастанию времени, с логинами участников."""
    users = sa.table("users", sa.column("id"), sa.column("login"))
    actor = users.alias("actor")
    subject = users.alias("subject")
    query = (
        sa.select(
            audit_log.c.id,
            audit_log.c.created_at,
            audit_log.c.event,
            actor.c.login.label("actor_login"),
            subject.c.login.label("subject_login"),
            audit_log.c.ip,
            audit_log.c.details,
        )
        .select_from(
            audit_log.outerjoin(actor, actor.c.id == audit_log.c.actor_id).outerjoin(
                subject, subject.c.id == audit_log.c.subject_user_id
            )
        )
        .order_by(audit_log.c.id.desc())
        .limit(limit)
    )
    if since is not None:
        query = query.where(audit_log.c.created_at >= since)
    if event is not None:
        query = query.where(audit_log.c.event == event)
    rows = (await connection.execute(query)).all()
    return list(reversed(rows))
