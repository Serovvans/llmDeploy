"""Хранилище диалогов на SQLAlchemy Core; условие владельца — в каждом запросе."""

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from portal.dialogs.domain import Attachment, Dialog, DialogKind, Message, MessageStatus
from portal.dialogs.ports import DialogRepository, DialogUnitOfWork
from portal.dialogs.tables import attachments, dialogs, messages

type _Row = sa.Row[*tuple[Any, ...]]

_MESSAGE_FIELDS = tuple(Message.__dataclass_fields__)
_MESSAGE_COLUMNS = [messages.c[name] for name in _MESSAGE_FIELDS]


def _dialog(row: _Row) -> Dialog:
    return Dialog(**row._asdict())


def _message(row: _Row) -> Message:
    return Message(**{name: getattr(row, name) for name in _MESSAGE_FIELDS})


def _attachment(row: _Row) -> Attachment:
    return Attachment(**{column.name: getattr(row, column.name) for column in attachments.c})


def _owned(owner_id: UUID) -> sa.Select[tuple[UUID]]:
    """Идентификаторы диалогов владельца — условие изоляции для дочерних таблиц."""
    return sa.select(dialogs.c.id).where(dialogs.c.owner_id == owner_id)


_HAS_MESSAGES = sa.exists().where(messages.c.dialog_id == dialogs.c.id)


class SqlDialogRepository:
    """Таблицы `dialogs`, `messages`, `attachments`."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Работать в транзакции переданного соединения."""
        self._connection = connection

    async def add_dialog(self, dialog: Dialog) -> None:
        """Создать диалог."""
        values = {column.name: getattr(dialog, column.name) for column in dialogs.c}
        await self._connection.execute(sa.insert(dialogs).values(**values))

    async def get_dialog(
        self, owner_id: UUID, dialog_id: UUID, *, lock: bool = False
    ) -> Dialog | None:
        """Диалог владельца; `lock` — `SELECT … FOR UPDATE`."""
        query = sa.select(dialogs).where(dialogs.c.id == dialog_id, dialogs.c.owner_id == owner_id)
        if lock:
            query = query.with_for_update()
        row = (await self._connection.execute(query)).first()
        return _dialog(row) if row else None

    async def list_dialogs(
        self, owner_id: UUID, kind: DialogKind, limit: int, before: tuple[datetime, UUID] | None
    ) -> list[Dialog]:
        """Диалоги владельца с сообщениями, по убыванию `updated_at`, после курсора."""
        query = sa.select(dialogs).where(
            dialogs.c.owner_id == owner_id, dialogs.c.kind == kind, _HAS_MESSAGES
        )
        if before is not None:
            query = query.where(sa.tuple_(dialogs.c.updated_at, dialogs.c.id) < sa.tuple_(*before))
        query = query.order_by(dialogs.c.updated_at.desc(), dialogs.c.id.desc()).limit(limit)
        return [_dialog(row) for row in await self._connection.execute(query)]

    async def stale_empty_dialogs(
        self, owner_id: UUID | None, created_before: datetime
    ) -> list[tuple[UUID, UUID]]:
        """Диалоги без сообщений старше срока: пары (владелец, диалог)."""
        query = sa.select(dialogs.c.owner_id, dialogs.c.id).where(
            dialogs.c.created_at < created_before, sa.not_(_HAS_MESSAGES)
        )
        if owner_id is not None:
            query = query.where(dialogs.c.owner_id == owner_id)
        return [(row.owner_id, row.id) for row in await self._connection.execute(query)]

    async def set_title(self, owner_id: UUID, dialog_id: UUID, title: str) -> None:
        """Переименовать диалог; `updated_at` не меняется."""
        await self._connection.execute(
            sa.update(dialogs)
            .where(dialogs.c.id == dialog_id, dialogs.c.owner_id == owner_id)
            .values(title=title)
        )

    async def set_title_if_empty(self, owner_id: UUID, dialog_id: UUID, title: str) -> bool:
        """Записать название, если его ещё нет."""
        result = await self._connection.execute(
            sa.update(dialogs)
            .where(
                dialogs.c.id == dialog_id,
                dialogs.c.owner_id == owner_id,
                dialogs.c.title.is_(None),
            )
            .values(title=title)
        )
        return result.rowcount > 0

    async def touch_dialog(self, owner_id: UUID, dialog_id: UUID, now: datetime) -> None:
        """Обновить `updated_at`."""
        await self._connection.execute(
            sa.update(dialogs)
            .where(dialogs.c.id == dialog_id, dialogs.c.owner_id == owner_id)
            .values(updated_at=now)
        )

    async def delete_dialog(self, owner_id: UUID, dialog_id: UUID) -> None:
        """Удалить диалог; сообщения и вложения уходят каскадом."""
        await self._connection.execute(
            sa.delete(dialogs).where(dialogs.c.id == dialog_id, dialogs.c.owner_id == owner_id)
        )

    def _messages_of(self, owner_id: UUID, dialog_id: UUID) -> sa.Select[Any]:
        return sa.select(*_MESSAGE_COLUMNS).where(
            messages.c.dialog_id == dialog_id, messages.c.dialog_id.in_(_owned(owner_id))
        )

    async def last_message(self, owner_id: UUID, dialog_id: UUID) -> Message | None:
        """Последнее сообщение диалога."""
        found = await self.list_messages(owner_id, dialog_id, 1, None)
        return found[0] if found else None

    async def list_messages(
        self, owner_id: UUID, dialog_id: UUID, limit: int, before_position: int | None
    ) -> list[Message]:
        """Сообщения от новых к старым, раньше позиции курсора."""
        query = self._messages_of(owner_id, dialog_id)
        if before_position is not None:
            query = query.where(messages.c.position < before_position)
        query = query.order_by(messages.c.position.desc()).limit(limit)
        return [_message(row) for row in await self._connection.execute(query)]

    async def history(self, owner_id: UUID, dialog_id: UUID, before_position: int) -> list[Message]:
        """Сообщения диалога до позиции, по порядку."""
        query = (
            self._messages_of(owner_id, dialog_id)
            .where(messages.c.position < before_position)
            .order_by(messages.c.position)
        )
        return [_message(row) for row in await self._connection.execute(query)]

    async def first_question(self, owner_id: UUID, dialog_id: UUID) -> str | None:
        """Текст первого непустого вопроса диалога."""
        query = (
            sa.select(messages.c.content)
            .where(
                messages.c.dialog_id == dialog_id,
                messages.c.dialog_id.in_(_owned(owner_id)),
                messages.c.role == "user",
                messages.c.content != "",
            )
            .order_by(messages.c.position)
            .limit(1)
        )
        content: str | None = await self._connection.scalar(query)
        return content

    async def message_exists(self, owner_id: UUID, message_id: UUID) -> bool:
        """Существует ли ещё сообщение."""
        found = await self._connection.scalar(
            sa.select(messages.c.id).where(
                messages.c.id == message_id, messages.c.dialog_id.in_(_owned(owner_id))
            )
        )
        return found is not None

    async def add_message(self, message: Message) -> None:
        """Создать сообщение."""
        values = {name: getattr(message, name) for name in _MESSAGE_FIELDS}
        await self._connection.execute(sa.insert(messages).values(**values))

    async def delete_message(self, owner_id: UUID, message_id: UUID) -> None:
        """Удалить сообщение."""
        await self._connection.execute(
            sa.delete(messages).where(
                messages.c.id == message_id, messages.c.dialog_id.in_(_owned(owner_id))
            )
        )

    async def finish_answer(
        self,
        owner_id: UUID,
        message_id: UUID,
        *,
        content: str,
        status: MessageStatus,
        error_code: str | None,
        reasoning: str | None,
        reasoning_seconds: int | None,
        sources: Sequence[Mapping[str, Any]] | None,
        sources_found: int | None,
        dropped_messages: int,
    ) -> None:
        """Сохранить ответ с итоговым состоянием."""
        await self._connection.execute(
            sa.update(messages)
            .where(messages.c.id == message_id, messages.c.dialog_id.in_(_owned(owner_id)))
            .values(
                content=content,
                status=status,
                error_code=error_code,
                reasoning=reasoning,
                reasoning_seconds=reasoning_seconds,
                sources=None if sources is None else [dict(source) for source in sources],
                sources_found=sources_found,
                dropped_messages=dropped_messages,
            )
        )

    async def interrupt_answer(self, owner_id: UUID, message_id: UUID) -> None:
        """Перевести ответ `streaming`, который никто не формирует, в `error`/`interrupted`."""
        await self._connection.execute(
            sa.update(messages)
            .where(
                messages.c.id == message_id,
                messages.c.dialog_id.in_(_owned(owner_id)),
                messages.c.status == "streaming",
            )
            .values(status="error", error_code="interrupted")
        )

    async def reset_streaming(self) -> None:
        """Перевести все ответы `streaming` в `error`/`interrupted`.

        Единственный запрос без условия владельца: служебный, выполняется при старте
        процесса и никому ничего не отдаёт.
        """
        await self._connection.execute(
            sa.update(messages)
            .where(messages.c.status == "streaming")
            .values(status="error", error_code="interrupted")
        )

    async def add_attachment(self, attachment: Attachment) -> None:
        """Создать вложение."""
        values = {column.name: getattr(attachment, column.name) for column in attachments.c}
        await self._connection.execute(sa.insert(attachments).values(**values))

    async def get_attachment(
        self, owner_id: UUID, dialog_id: UUID, attachment_id: UUID
    ) -> Attachment | None:
        """Вложение диалога владельца."""
        row = (
            await self._connection.execute(
                sa.select(attachments).where(
                    attachments.c.id == attachment_id,
                    attachments.c.dialog_id == dialog_id,
                    attachments.c.dialog_id.in_(_owned(owner_id)),
                )
            )
        ).first()
        return _attachment(row) if row else None

    async def delete_attachment(self, owner_id: UUID, attachment_id: UUID) -> None:
        """Удалить вложение."""
        await self._connection.execute(
            sa.delete(attachments).where(
                attachments.c.id == attachment_id, attachments.c.dialog_id.in_(_owned(owner_id))
            )
        )

    async def dialog_attachments(self, owner_id: UUID, dialog_id: UUID) -> list[Attachment]:
        """Все вложения диалога: отправленные и нет."""
        rows = await self._connection.execute(
            sa.select(attachments)
            .where(
                attachments.c.dialog_id == dialog_id, attachments.c.dialog_id.in_(_owned(owner_id))
            )
            .order_by(attachments.c.created_at, attachments.c.id)
        )
        return [_attachment(row) for row in rows]

    async def message_attachments(
        self, owner_id: UUID, message_ids: Sequence[UUID]
    ) -> dict[UUID, list[Attachment]]:
        """Вложения сообщений, по идентификатору сообщения, в порядке загрузки."""
        grouped: dict[UUID, list[Attachment]] = {}
        if not message_ids:
            return grouped
        rows = await self._connection.execute(
            sa.select(attachments)
            .where(
                attachments.c.message_id.in_(message_ids),
                attachments.c.dialog_id.in_(_owned(owner_id)),
            )
            .order_by(attachments.c.created_at, attachments.c.id)
        )
        for row in rows:
            grouped.setdefault(row.message_id, []).append(_attachment(row))
        return grouped

    async def attach(
        self, owner_id: UUID, attachment_ids: Sequence[UUID], message_id: UUID
    ) -> None:
        """Привязать вложения к отправленному вопросу."""
        if attachment_ids:
            await self._connection.execute(
                sa.update(attachments)
                .where(
                    attachments.c.id.in_(attachment_ids),
                    attachments.c.dialog_id.in_(_owned(owner_id)),
                )
                .values(message_id=message_id)
            )


class SqlDialogUnitOfWork:
    """Хранилище на одном соединении."""

    def __init__(self, connection: AsyncConnection) -> None:
        """Собрать хранилище вокруг соединения."""
        self._connection = connection
        self.dialogs: DialogRepository = SqlDialogRepository(connection)

    async def commit(self) -> None:
        """Зафиксировать сделанное."""
        await self._connection.commit()


class SqlDialogUnitOfWorkFactory:
    """Открывает единицу работы на соединении из пула."""

    def __init__(self, engine: AsyncEngine) -> None:
        """Запомнить движок."""
        self._engine = engine

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[DialogUnitOfWork]:
        """Фиксация при выходе без ошибки; при ошибке незафиксированное откатывается."""
        async with self._engine.connect() as connection:
            yield SqlDialogUnitOfWork(connection)
            await connection.commit()
