"""Порты модуля диалогов: хранилище и единица работы.

Каждый метод с пользовательскими данными принимает идентификатор владельца и включает
условие `dialogs.owner_id = :owner` в сам запрос (docs/portal-api.md §9.15).
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from portal.dialogs.context import Turn
from portal.dialogs.domain import (
    Attachment,
    Dialog,
    DialogKind,
    Docparse,
    Message,
    MessageStatus,
    SummaryStatus,
)
from portal.kb.ports import KnowledgeScope
from portal.llm.ports import ReasoningEffort


class DialogRepository(Protocol):
    """Диалоги с сообщениями и вложениями."""

    async def add_dialog(self, dialog: Dialog) -> None:
        """Создать диалог."""
        ...

    async def get_dialog(
        self, owner_id: UUID, dialog_id: UUID, *, lock: bool = False
    ) -> Dialog | None:
        """Диалог владельца; `lock` — держать строку до конца транзакции.

        Скрытый диалог разбора (таблицы реквизитов ещё нет) не виден ни здесь, ни в списке.
        """
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
        sql_check: Mapping[str, Any] | None,
        dropped_messages: int,
    ) -> None:
        """Сохранить ответ с итоговым состоянием."""
        ...

    async def interrupt_answer(self, owner_id: UUID, message_id: UUID) -> None:
        """Перевести ответ `streaming`, который никто не формирует, в `error`/`interrupted`."""
        ...

    async def set_question_params(
        self, owner_id: UUID, message_id: UUID, params: Mapping[str, Any]
    ) -> None:
        """Заменить сохранённые параметры вопроса (схема SQL при повторной генерации)."""
        ...

    async def add_docparse(self, docparse: Docparse) -> None:
        """Создать разбор вместе с его скрытым диалогом."""
        ...

    async def get_docparse(self, owner_id: UUID, dialog_id: UUID) -> Docparse | None:
        """Готовый разбор владельца; скрытый не отдаётся."""
        ...

    async def publish_docparse(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        *,
        fields: Sequence[Mapping[str, Any]],
        document_text: str,
        title: str,
        now: datetime,
    ) -> None:
        """Сохранить таблицу и текст, назвать диалог и сделать разбор видимым (§7.3, шаг 4)."""
        ...

    async def finish_summary(
        self, owner_id: UUID, dialog_id: UUID, summary: str, status: SummaryStatus
    ) -> None:
        """Сохранить краткое содержание с итоговым состоянием; пишется один раз."""
        ...

    async def docparse_exists(self, owner_id: UUID, dialog_id: UUID) -> bool:
        """Существует ли ещё разбор (скрытый тоже): удаление диалога обрывает его поток."""
        ...

    async def delete_hidden_docparse(self, owner_id: UUID, dialog_id: UUID) -> str | None:
        """Удалить скрытый диалог разбора; вернуть ключ его файла, если диалог был."""
        ...

    async def reset_docparses(self) -> list[str]:
        """Старт процесса: зависшие краткие содержания — в `error`, скрытые разборы удалить.

        Возвращает ключи файлов удалённых разборов.
        """
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


@dataclass(frozen=True)
class AnswerPlan:
    """Из чего собирается запрос к модели на один вопрос (§5.4).

    `scope` — область поиска в базе знаний, `None` — поиск не выполняется. `sql_dialect`
    задан у диалога `sql`: по нему проверяются блоки `sql` готового ответа.
    """

    system: str
    question: Turn
    effort: ReasoningEffort
    max_tokens: int
    scope: KnowledgeScope | None = None
    sql_dialect: str | None = None


class DialogTool(Protocol):
    """Правила вида диалога, отличного от чата: `sql`, `cogis`, `docparse` (§5.3, §7)."""

    async def accept(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> list[str] | None:
        """Проверки до открытия потока; вернуть `sql_dangers` вопроса (`None` — нечего)."""
        ...

    async def plan(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> AnswerPlan:
        """Системное сообщение, вопрос с блоками данных и параметры запроса к модели."""
        ...

    async def review(self, plan: AnswerPlan, content: str) -> Mapping[str, Any] | None:
        """Проверка готового ответа (`SqlCheck`); `None` — у этого вида её нет."""
        ...
