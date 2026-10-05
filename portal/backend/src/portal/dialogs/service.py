"""Сценарии диалогов: список, переименование, удаление, вложения (docs/portal-api.md §5)."""

import asyncio
import base64
import binascii
import json
import re
from collections.abc import AsyncIterator, Callable, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from uuid import UUID, uuid4

from portal.core.errors import (
    field_error,
    file_too_large,
    file_unreadable,
    not_found,
    too_many_pages,
    unsupported_file_type,
    validation_error,
)
from portal.core.ports import Clock
from portal.core.settings import ChatSettings
from portal.dialogs import errors
from portal.dialogs.domain import (
    TITLE_MAX_LENGTH,
    Attachment,
    Dialog,
    DialogKind,
    Docparse,
    Message,
)
from portal.dialogs.ports import DialogUnitOfWorkFactory
from portal.files.names import display_file_name
from portal.files.ports import (
    DocumentReader,
    FileStorage,
    FileTooLargeError,
    MediaType,
    UnreadableDocumentError,
)
from portal.files.text import store_as_utf8
from portal.llm.ports import MAX_IMAGES_PER_REQUEST

_TEXT_TYPES = ("text/plain", "text/markdown")
_ALL_POSITIONS = 2**31 - 1
# Название раздела — запасное имя файла экспорта, когда у диалога нет названия (§5.9).
_SECTION_TITLES: dict[DialogKind, str] = {
    "chat": "Чат",
    "sql": "SQL-помощник",
    "cogis": "Помощник CoGIS",
    "docparse": "Разбор документа",
}
_FORBIDDEN_IN_FILE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
# Окончания названия, которые считаются расширением загруженного файла (§5.9).
_FILE_EXTENSIONS = (".pdf", ".docx", ".txt", ".md", ".jpg", ".jpeg", ".png")


def export_file_name(title: str) -> str:
    """Имя файла экспорта по названию диалога (§5.9).

    Название разбора — имя загруженного файла: одно его расширение отбрасывается, если
    название после этого не пусто («выписка.docx», а не «выписка.docx.docx»).
    """
    name = _FORBIDDEN_IN_FILE_NAME.sub("_", title).strip()
    extension = next((item for item in _FILE_EXTENSIONS if name.lower().endswith(item)), "")
    if extension and name[: -len(extension)].strip():
        name = name[: -len(extension)].strip()
    return f"{name or 'dialog'}.docx"


@dataclass(frozen=True)
class MessageView:
    """Сообщение и его вложения."""

    message: Message
    attachments: list[Attachment]


@dataclass(frozen=True)
class CursorPage[T]:
    """Часть списка «Показать ещё»; `next_cursor = None` — дальше ничего нет."""

    items: list[T]
    next_cursor: str | None


@dataclass(frozen=True)
class _FileFacts:
    media_type: MediaType
    page_count: int | None
    text_content: str | None
    image_pages: list[int]
    size_bytes: int


def _encode_cursor(*values: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(values, default=str).encode()).decode()


def _decode_cursor(cursor: str) -> list[object]:
    try:
        values = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (ValueError, binascii.Error) as error:
        raise validation_error([field_error("cursor", "invalid_format")]) from error
    if not isinstance(values, list):
        raise validation_error([field_error("cursor", "invalid_format")])
    return values


class DialogExporter(Protocol):
    """Сборка файла экспорта; реализация — на `python-docx`."""

    def answer(self, title: str, view: MessageView) -> bytes:
        """Файл с одним ответом."""
        ...

    def dialog(
        self, title: str, dialog: Dialog, docparse: Docparse | None, views: Sequence[MessageView]
    ) -> bytes:
        """Файл с диалогом целиком."""
        ...


class DialogService:
    """Диалоги и вложения владельца; чужое всегда `not_found`."""

    def __init__(
        self,
        uow_factory: DialogUnitOfWorkFactory,
        storage: FileStorage,
        reader: DocumentReader,
        document_executor: Executor,
        clock: Clock,
        settings: ChatSettings,
        empty_ttl_hours: int,
        text_max_chars: int,
        is_forming: Callable[[UUID], bool],
        summary_forming: Callable[[UUID], bool],
        exporter: DialogExporter,
    ) -> None:
        """Получить зависимости явно.

        `text_max_chars` — сколько символов текста вложения хранить: больше заведомо не
        поместится в запрос к модели, сколько бы ни развернулось из файла. `is_forming`
        отвечает, формируется ли ответ прямо сейчас, `summary_forming` — пишется ли краткое
        содержание разбора. `document_executor` — потоки чтения документов: ждущие
        блокировку PDF стоят в их очереди и не занимают общий пул цикла событий.
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._reader = reader
        self._document_executor = document_executor
        self._clock = clock
        self._settings = settings
        self._empty_ttl = timedelta(hours=empty_ttl_hours)
        self._text_max_chars = text_max_chars
        self._is_forming = is_forming
        self._summary_forming = summary_forming
        self._exporter = exporter

    async def reset_interrupted(self) -> None:
        """При старте процесса убрать следы прерванной работы (§5.5, «Зависший ответ»).

        Ответы `streaming` становятся `interrupted`, недописанные краткие содержания —
        `error`, скрытые диалоги разбора удаляются вместе с файлами.
        """
        async with self._uow_factory() as uow:
            await uow.dialogs.reset_streaming()
            orphans = await uow.dialogs.reset_docparses()
        for key in orphans:
            await self._storage.delete(key)

    # --- диалоги ---

    async def purge_empty(self, owner_id: UUID | None = None) -> None:
        """Удалить диалоги без сообщений старше `dialogs.empty_ttl_hours` с их вложениями.

        Без владельца — у всех (старт процесса); с владельцем — у создающего новый диалог.
        """
        async with self._uow_factory() as uow:
            stale = await uow.dialogs.stale_empty_dialogs(
                owner_id, self._clock.now() - self._empty_ttl
            )
        for owner, dialog_id in stale:
            await self._delete(owner, dialog_id, only_empty=True)

    async def create(self, owner_id: UUID, kind: DialogKind) -> Dialog:
        """Создать пустой диалог; заодно убрать брошенные пустые диалоги владельца."""
        await self.purge_empty(owner_id)
        now = self._clock.now()
        dialog = Dialog(uuid4(), owner_id, kind, None, now, now)
        async with self._uow_factory() as uow:
            await uow.dialogs.add_dialog(dialog)
        return dialog

    async def get(self, owner_id: UUID, dialog_id: UUID) -> Dialog:
        """Диалог владельца."""
        return (await self.get_with_docparse(owner_id, dialog_id))[0]

    async def get_with_docparse(
        self, owner_id: UUID, dialog_id: UUID
    ) -> tuple[Dialog, Docparse | None]:
        """Диалог владельца и, у вида `docparse`, результат разбора (§7.3)."""
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
            if dialog is None:
                raise not_found()
            docparse = None
            if dialog.kind == "docparse":
                docparse = await uow.dialogs.get_docparse(owner_id, dialog_id)
                stuck = docparse is not None and docparse.summary_status == "streaming"
                if docparse is not None and stuck and not self._summary_forming(dialog_id):
                    # Поток разбора уже не идёт: содержание осталось неполным. Интерфейс в
                    # состоянии «пишется» сам из него не выйдет.
                    docparse.summary_status = "error"
                    await uow.dialogs.finish_summary(owner_id, dialog_id, docparse.summary, "error")
        return dialog, docparse

    async def list(
        self, owner_id: UUID, kind: DialogKind, limit: int, cursor: str | None
    ) -> CursorPage[Dialog]:
        """Диалоги одного вида по убыванию `updated_at`."""
        before = None
        if cursor is not None:
            try:
                updated_at, dialog_id = _decode_cursor(cursor)
                before = (datetime.fromisoformat(str(updated_at)), UUID(str(dialog_id)))
            except ValueError as error:
                raise validation_error([field_error("cursor", "invalid_format")]) from error
        async with self._uow_factory() as uow:
            items = await uow.dialogs.list_dialogs(owner_id, kind, limit + 1, before)
        more = len(items) > limit
        items = items[:limit]
        last = items[-1] if items else None
        next_cursor = (
            _encode_cursor(last.updated_at.isoformat(), last.id) if more and last else None
        )
        return CursorPage(items, next_cursor)

    async def rename(self, owner_id: UUID, dialog_id: UUID, title: str) -> Dialog:
        """Переименовать диалог; `updated_at` не меняется."""
        title = title.strip()
        if not title:
            raise validation_error([field_error("title", "required")])
        if len(title) > TITLE_MAX_LENGTH:
            raise validation_error([field_error("title", "too_long")])
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True)
            if dialog is None:
                raise not_found()
            await uow.dialogs.set_title(owner_id, dialog_id, title)
            dialog.title = title
            return dialog

    async def delete(self, owner_id: UUID, dialog_id: UUID) -> None:
        """Удалить диалог: сначала файлы вложений, затем строка (§5.2).

        Формируемый в нём ответ оборвётся сам: его задача увидит, что сообщения нет.
        """
        if not await self._delete(owner_id, dialog_id, only_empty=False):
            raise not_found()

    async def _delete(self, owner_id: UUID, dialog_id: UUID, *, only_empty: bool) -> bool:
        """Удалить диалог одной транзакцией под блокировкой его строки.

        Загрузка вложения берёт ту же блокировку перед записью строки, поэтому либо её
        файл попадёт в список на удаление, либо она увидит, что диалога уже нет, и сама
        уберёт принятый файл. Сбой при удалении файла откатывает транзакцию: диалог
        остаётся видимым, удаление повторяется.
        """
        async with self._uow_factory() as uow:
            if await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True) is None:
                return False
            if only_empty and await uow.dialogs.last_message(owner_id, dialog_id) is not None:
                return False
            for attachment in await uow.dialogs.dialog_attachments(owner_id, dialog_id):
                await self._storage.delete(attachment.storage_key)
            docparse = await uow.dialogs.get_docparse(owner_id, dialog_id)
            if docparse is not None:
                await self._storage.delete(docparse.storage_key)
            await uow.dialogs.delete_dialog(owner_id, dialog_id)
        return True

    async def export(
        self, owner_id: UUID, dialog_id: UUID, message_id: UUID | None
    ) -> tuple[str, bytes]:
        """Имя файла и содержимое DOCX: один ответ или диалог целиком (§5.9)."""
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
            if dialog is None:
                raise not_found()
            docparse = await uow.dialogs.get_docparse(owner_id, dialog_id)
            found = await uow.dialogs.history(owner_id, dialog_id, _ALL_POSITIONS)
            files = await uow.dialogs.message_attachments(
                owner_id, [message.id for message in found if message.role == "user"]
            )
        views = [MessageView(message, files.get(message.id, [])) for message in found]
        title = dialog.title or f"{_SECTION_TITLES[dialog.kind]} {dialog.created_at:%Y-%m-%d}"
        if message_id is not None:
            answer = next(
                (
                    view
                    for view in views
                    if view.message.id == message_id and view.message.role == "assistant"
                ),
                None,
            )
            if answer is None:
                raise not_found()
            content = await asyncio.to_thread(self._exporter.answer, title, answer)
        elif not views and docparse is None:
            raise errors.nothing_to_export()
        else:
            content = await asyncio.to_thread(self._exporter.dialog, title, dialog, docparse, views)
        return export_file_name(title), content

    async def messages(
        self, owner_id: UUID, dialog_id: UUID, limit: int, cursor: str | None
    ) -> CursorPage[MessageView]:
        """Сообщения диалога от новых к старым."""
        before = None
        if cursor is not None:
            values = _decode_cursor(cursor)
            if len(values) != 1 or not isinstance(values[0], int):
                raise validation_error([field_error("cursor", "invalid_format")])
            before = values[0]
        async with self._uow_factory() as uow:
            if await uow.dialogs.get_dialog(owner_id, dialog_id) is None:
                raise not_found()
            found = await uow.dialogs.list_messages(owner_id, dialog_id, limit + 1, before)
            more = len(found) > limit
            found = found[:limit]
            for message in found:
                # «Формируется» без живой задачи — ответ, который не удалось сохранить:
                # интерфейс в этом состоянии только перечитывает сообщения и сам из него
                # не выйдет, поэтому ответ объявляется прерванным уже при чтении (§5.5).
                if message.status == "streaming" and not self._is_forming(message.id):
                    message.status, message.error_code = "error", "interrupted"
                    await uow.dialogs.interrupt_answer(owner_id, message.id)
            files = await uow.dialogs.message_attachments(
                owner_id, [message.id for message in found if message.role == "user"]
            )
        items = [MessageView(message, files.get(message.id, [])) for message in found]
        next_cursor = _encode_cursor(found[-1].position) if more else None
        return CursorPage(items, next_cursor)

    # --- вложения ---

    async def require_chat(self, owner_id: UUID, dialog_id: UUID) -> None:
        """Проверить до приёма файла: диалог есть, он свой и это чат."""
        dialog = await self.get(owner_id, dialog_id)
        if dialog.kind != "chat":
            raise errors.wrong_dialog_kind()

    async def add_attachment(
        self, owner_id: UUID, dialog_id: UUID, file_name: str, chunks: AsyncIterator[bytes]
    ) -> Attachment:
        """Принять файл, определить тип по содержимому, извлечь текст и страницы-сканы."""
        await self.require_chat(owner_id, dialog_id)
        limit = self._settings.attachment_max_bytes
        try:
            stored = await self._storage.save("attachments", chunks, limit)
        except FileTooLargeError as error:
            raise file_too_large(limit) from error
        try:
            facts = await asyncio.get_running_loop().run_in_executor(
                self._document_executor,
                self._inspect,
                self._storage.path(stored.key),
                file_name,
                stored.size_bytes,
            )
            attachment = Attachment(
                id=uuid4(),
                dialog_id=dialog_id,
                message_id=None,
                file_name=display_file_name(file_name),
                media_type=facts.media_type,
                size_bytes=facts.size_bytes,
                storage_key=stored.key,
                page_count=facts.page_count,
                text_content=facts.text_content,
                image_pages=facts.image_pages,
                created_at=self._clock.now(),
            )
            evicted: list[Attachment] = []
            async with self._uow_factory() as uow:
                # Диалог могли удалить, пока шёл приём файла.
                if await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True) is None:
                    raise not_found()
                # Неотправленных не больше chat.max_attachments: лишнее старое вытесняется.
                files = await uow.dialogs.dialog_attachments(owner_id, dialog_id)
                unsent = [item for item in files if item.message_id is None]
                evicted = unsent[: max(0, len(unsent) + 1 - self._settings.max_attachments)]
                for old in evicted:
                    await uow.dialogs.delete_attachment(owner_id, old.id)
                await uow.dialogs.add_attachment(attachment)
        except BaseException:
            await self._storage.delete(stored.key)
            raise
        for old in evicted:
            await self._storage.delete(old.storage_key)
        return attachment

    def _inspect(self, path: Path, file_name: str, size_bytes: int) -> _FileFacts:
        """Разобрать принятый файл; блокирующая работа — вызывается в пуле потоков."""
        media_type = self._reader.detect(path, file_name)
        if media_type is None:
            raise unsupported_file_type()
        try:
            page_count = self._reader.page_count(path, media_type)
            if media_type == "application/pdf" and page_count is not None:
                max_pages = self._settings.attachment_max_pages
                if page_count > max_pages:
                    raise too_many_pages(max_pages)
                return self._inspect_pdf(path, page_count, size_bytes)
            text = self._reader.page_text(path, media_type, 1)
        except UnreadableDocumentError as error:
            raise file_unreadable() from error
        if media_type in _TEXT_TYPES:
            # Текстовые файлы хранятся в UTF-8, в какой бы кодировке ни пришли (§1.5).
            size_bytes = store_as_utf8(path)
        if text is not None:
            text = text[: self._text_max_chars]
        image_pages = [1] if text is None else []
        return _FileFacts(media_type, page_count, text, image_pages, size_bytes)

    def _inspect_pdf(self, path: Path, page_count: int, size_bytes: int) -> _FileFacts:
        texts: list[str] = []
        scans: list[int] = []
        length = 0
        for page in range(1, page_count + 1):
            # Текста уже больше, чем помещается в запрос к модели: дальше читать незачем,
            # такое вложение всё равно получит отказ message_too_long при отправке.
            if length >= self._text_max_chars:
                break
            text = self._reader.page_text(path, "application/pdf", page)
            if text is None:
                scans.append(page)
                # Скан длиннее 8 страниц отклоняется сразу: дальше читать незачем (§5.8).
                if len(scans) > MAX_IMAGES_PER_REQUEST:
                    raise errors.too_many_images()
            else:
                texts.append(f"[стр. {page}]\n{text}")
                length += len(text)
        content = "\n\n".join(texts)[: self._text_max_chars] or None
        return _FileFacts("application/pdf", page_count, content, scans, size_bytes)

    async def get_attachment(
        self, owner_id: UUID, dialog_id: UUID, attachment_id: UUID
    ) -> tuple[Attachment, Path]:
        """Вложение владельца и путь к его оригиналу."""
        async with self._uow_factory() as uow:
            attachment = await uow.dialogs.get_attachment(owner_id, dialog_id, attachment_id)
        if attachment is None:
            raise not_found()
        return attachment, self._storage.path(attachment.storage_key)

    async def delete_attachment(self, owner_id: UUID, dialog_id: UUID, attachment_id: UUID) -> None:
        """Удалить неотправленное вложение."""
        async with self._uow_factory() as uow:
            if await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True) is None:
                raise not_found()
            attachment = await uow.dialogs.get_attachment(owner_id, dialog_id, attachment_id)
            if attachment is None:
                raise not_found()
            if attachment.message_id is not None:
                raise errors.attachment_already_sent()
            await self._storage.delete(attachment.storage_key)
            await uow.dialogs.delete_attachment(owner_id, attachment_id)
