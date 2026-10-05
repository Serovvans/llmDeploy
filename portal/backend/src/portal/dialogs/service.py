"""Сценарии диалогов: список, переименование, удаление, вложения (docs/portal-api.md §5)."""

import asyncio
import base64
import binascii
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from portal.core.errors import (
    field_error,
    file_too_large,
    not_found,
    unsupported_file_type,
    validation_error,
)
from portal.core.ports import Clock
from portal.core.settings import ChatSettings
from portal.dialogs import errors
from portal.dialogs.domain import TITLE_MAX_LENGTH, Attachment, Dialog, DialogKind, Message
from portal.dialogs.ports import DialogUnitOfWorkFactory
from portal.files.ports import (
    DocumentReader,
    FileStorage,
    FileTooLargeError,
    MediaType,
    UnreadableDocumentError,
)
from portal.files.text import decode_text
from portal.llm.ports import MAX_IMAGES_PER_REQUEST

_FILE_NAME_MAX_LENGTH = 255
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_TEXT_TYPES = ("text/plain", "text/markdown")


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


def display_file_name(raw: str) -> str:
    """Имя для показа: последняя часть пути, без управляющих символов, до 255 символов."""
    name = _CONTROL_CHARACTERS.sub("", raw.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:_FILE_NAME_MAX_LENGTH] or "файл"


class DialogService:
    """Диалоги и вложения владельца; чужое всегда `not_found`."""

    def __init__(
        self,
        uow_factory: DialogUnitOfWorkFactory,
        storage: FileStorage,
        reader: DocumentReader,
        clock: Clock,
        settings: ChatSettings,
        empty_ttl_hours: int,
        text_max_chars: int,
        is_forming: Callable[[UUID], bool],
    ) -> None:
        """Получить зависимости явно.

        `text_max_chars` — сколько символов текста вложения хранить: больше заведомо не
        поместится в запрос к модели, сколько бы ни развернулось из файла. `is_forming`
        отвечает, формируется ли ответ прямо сейчас.
        """
        self._uow_factory = uow_factory
        self._storage = storage
        self._reader = reader
        self._clock = clock
        self._settings = settings
        self._empty_ttl = timedelta(hours=empty_ttl_hours)
        self._text_max_chars = text_max_chars
        self._is_forming = is_forming

    async def reset_interrupted(self) -> None:
        """При старте процесса перевести зависшие ответы в ошибку `interrupted` (§5.5)."""
        async with self._uow_factory() as uow:
            await uow.dialogs.reset_streaming()

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
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
        if dialog is None:
            raise not_found()
        return dialog

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
            await uow.dialogs.delete_dialog(owner_id, dialog_id)
        return True

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
                    await uow.dialogs.finish_answer(
                        owner_id,
                        message.id,
                        content=message.content,
                        status="error",
                        error_code="interrupted",
                        reasoning=message.reasoning,
                        reasoning_seconds=message.reasoning_seconds,
                        dropped_messages=message.dropped_messages,
                    )
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
            facts = await asyncio.to_thread(
                self._inspect, self._storage.path(stored.key), file_name, stored.size_bytes
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
                    raise errors.too_many_pages(max_pages)
                return self._inspect_pdf(path, page_count, size_bytes)
            text = self._reader.page_text(path, media_type, 1)
        except UnreadableDocumentError as error:
            raise errors.file_unreadable() from error
        if media_type in _TEXT_TYPES:
            # Текстовые файлы хранятся в UTF-8, в какой бы кодировке ни пришли (§1.5).
            encoded = (decode_text(path.read_bytes()) or "").encode()
            path.write_bytes(encoded)
            size_bytes = len(encoded)
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
