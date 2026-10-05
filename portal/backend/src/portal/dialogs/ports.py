"""Порты модуля диалогов: хранилище и единица работы.

Каждый метод с пользовательскими данными принимает идентификатор владельца и включает
условие `dialogs.owner_id = :owner` в сам запрос (docs/portal-api.md §9.15).
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from portal.dialogs.domain import Attachment, Dialog, DialogKind, Message, MessageStatus


class DialogRepository(Protocol):
    """Диалоги с сообщениями и вложениями."""

    async def add_dialog(self, dialog: Dialog) -> None:
        """Создать диалог."""
        ...

    async def get_dialog(
        self, owner_id: UUID, dialog_id: UUID, *, lock: bool = False
    ) -> Dialog | None:
        """Диалог владельца; `lock` — держать строку до конца транзакции."""
        ...

    async def list_dialogs(
        self, owner_id: UUID, kind: DialogKind, limit: int, before: tuple[datetime, UUID] | None
    ) -> list[Dialog]:
        """Диалоги владельца с сообщениями, по убыванию `updated_at`, после курсора."""
        ...

    async def stale_empty_dialogs(
        self, owner_id: UUID | None, created_before: datetime
    ) -> list[tuple[UUID, UUID]]:
        """Диалоги без сообщений старше срока: пары (владелец, диалог).

        `owner_id = None` — у всех: служебная уборка при старте процесса.
        """
        ...

    async def set_title(self, owner_id: UUID, dialog_id: UUID, title: str) -> None:
        """Переименовать диалог; `updated_at` не меняется."""
        ...

    async def set_title_if_empty(self, owner_id: UUID, dialog_id: UUID, title: str) -> bool:
        """Записать название, если его ещё нет; `False` — пользователь уже назвал сам."""
        ...

    async def touch_dialog(self, owner_id: UUID, dialog_id: UUID, now: datetime) -> None:
        """Обновить `updated_at`: в диалоге новое сообщение."""
        ...

    async def delete_dialog(self, owner_id: UUID, dialog_id: UUID) -> None:
        """Удалить диалог; сообщения и вложения уходят каскадом."""
        ...

    async def last_message(self, owner_id: UUID, dialog_id: UUID) -> Message | None:
        """Последнее сообщение диалога."""
        ...

    async def list_messages(
        self, owner_id: UUID, dialog_id: UUID, limit: int, before_position: int | None
    ) -> list[Message]:
        """Сообщения от новых к старым, раньше позиции курсора."""
        ...

    async def history(self, owner_id: UUID, dialog_id: UUID, before_position: int) -> list[Message]:
        """Сообщения диалога до позиции, по порядку."""
        ...

    async def first_question(self, owner_id: UUID, dialog_id: UUID) -> str | None:
        """Текст первого непустого вопроса диалога."""
        ...

    async def message_exists(self, owner_id: UUID, message_id: UUID) -> bool:
        """Существует ли ещё сообщение: удаление диалога обрывает его ответ."""
        ...

    async def add_message(self, message: Message) -> None:
        """Создать сообщение."""
        ...

    async def delete_message(self, owner_id: UUID, message_id: UUID) -> None:
        """Удалить сообщение (прежний ответ при повторной генерации)."""
        ...

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
        ...

    async def interrupt_answer(self, owner_id: UUID, message_id: UUID) -> None:
        """Перевести ответ `streaming`, который никто не формирует, в `error`/`interrupted`."""
        ...

    async def reset_streaming(self) -> None:
        """Перевести все ответы `streaming` в `error`/`interrupted` (старт процесса)."""
        ...

    async def add_attachment(self, attachment: Attachment) -> None:
        """Создать вложение."""
        ...

    async def get_attachment(
        self, owner_id: UUID, dialog_id: UUID, attachment_id: UUID
    ) -> Attachment | None:
        """Вложение диалога владельца."""
        ...

    async def delete_attachment(self, owner_id: UUID, attachment_id: UUID) -> None:
        """Удалить вложение."""
        ...

    async def dialog_attachments(self, owner_id: UUID, dialog_id: UUID) -> list[Attachment]:
        """Все вложения диалога: отправленные и нет."""
        ...

    async def message_attachments(
        self, owner_id: UUID, message_ids: Sequence[UUID]
    ) -> dict[UUID, list[Attachment]]:
        """Вложения сообщений, по идентификатору сообщения, в порядке загрузки."""
        ...

    async def attach(
        self, owner_id: UUID, attachment_ids: Sequence[UUID], message_id: UUID
    ) -> None:
        """Привязать вложения к отправленному вопросу."""
        ...


class DialogUnitOfWork(Protocol):
    """Хранилище одной транзакции."""

    dialogs: DialogRepository

    async def commit(self) -> None:
        """Зафиксировать сделанное."""
        ...


class DialogUnitOfWorkFactory(Protocol):
    """Открывает единицу работы: фиксация при выходе без ошибки, иначе откат."""

    def __call__(self) -> AbstractAsyncContextManager[DialogUnitOfWork]:
        """Начать транзакцию."""
        ...
