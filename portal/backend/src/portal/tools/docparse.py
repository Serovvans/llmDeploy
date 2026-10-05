"""Разбор документа: чтение страниц, реквизиты по схеме шаблона, краткое содержание (§7.3).

Разбор идёт внутри запроса, в собственной задаче, не привязанной к соединению: закрытие
соединения — остановка. До готовности таблицы реквизитов ничего не сохраняется (скрытый
диалог и файл удаляются); с события `extraction` разбор есть в истории.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from portal.core.errors import (
    field_error,
    file_too_large,
    file_unreadable,
    too_many_pages,
    unsupported_file_type,
    validation_error,
)
from portal.core.events import EventChannel
from portal.core.logging import code_locations
from portal.core.ports import Clock, CurrentUser, SessionAuthenticator
from portal.core.settings import DocparseTemplate, Settings
from portal.dialogs.domain import Dialog, Docparse, SummaryStatus
from portal.dialogs.ports import DialogUnitOfWorkFactory
from portal.dialogs.watch import INTERRUPTION_MESSAGES, Interruption, StreamWatch, watched_stream
from portal.files.names import display_file_name
from portal.files.ports import (
    DOCX,
    DocumentReader,
    FileStorage,
    FileTooLargeError,
    MediaType,
    UnreadableDocumentError,
)
from portal.kb.ports import PageRecognizer, RecognitionFailedError
from portal.llm.ports import (
    MAX_IMAGES_PER_REQUEST,
    ChatModel,
    ChatRequest,
    ContentDelta,
    ContextOverflowError,
    Finished,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    TextPart,
    TokenEstimator,
)
from portal.tools import errors
from portal.tools.dialog_tools import document_block, fields_block

logger = logging.getLogger(__name__)

_SAVE_ATTEMPTS = 4
_SAVE_RETRY_SECONDS = 0.5
_MEDIA_TYPES: tuple[MediaType, ...] = ("image/jpeg", "image/png", "application/pdf", DOCX)
_ERROR_MESSAGES = {
    "recognition_failed": "В документе не нашлось текста.",
    "document_too_long": "Документ слишком длинный для разбора. Разделите его на части.",
    "model_unavailable": "Модель сейчас недоступна. Повторите попытку позже.",
    "model_overloaded": "Модель перегружена. Повторите попытку позже.",
    "internal_error": "Внутренняя ошибка. Повторите попытку позже.",
    **INTERRUPTION_MESSAGES,
}


class _RunFailedError(Exception):
    """Разбор завершается событием `error` с этим кодом."""

    def __init__(self, code: str, page: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.page = page


class _RunStoppedError(Exception):
    """Клиент закрыл соединение: завершающего события нет."""


@dataclass
class _Run:
    """Один идущий разбор."""

    owner_id: UUID
    session_id: UUID
    dialog_id: UUID
    template: DocparseTemplate
    file_name: str
    media_type: MediaType
    storage_key: str
    page_count: int | None
    # Текст страниц, известный без модели; `None` — страница-скан, её ещё распознавать.
    pages: list[str | None]
    channel: EventChannel = field(default_factory=EventChannel)
    published: bool = False


def template_schema(template: DocparseTemplate) -> dict[str, Any]:
    """JSON-схема структурированного вывода по шаблону (§7.3)."""
    if template.free_form:
        pair = {
            "type": "object",
            "properties": {"title": {"type": "string"}, "value": {"type": "string"}},
            "required": ["title", "value"],
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {"items": {"type": "array", "items": pair}},
            "required": ["items"],
            "additionalProperties": False,
        }
    properties = {
        item.name: {"type": "string", "description": item.description or item.title}
        for item in template.fields
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _value(raw: object) -> str | None:
    """Значение реквизита; пустое остаётся пустым, а не придумывается."""
    text = raw.strip() if isinstance(raw, str) else ""
    return text or None


def extracted_fields(
    template: DocparseTemplate, answer: str, free_form_max_items: int
) -> list[dict[str, Any]] | None:
    """Таблица реквизитов из ответа модели; `None` — ответ не разобрать.

    У шаблона со списком полей — в порядке шаблона; реквизит, которого модель не
    вернула или вернула пустым, получает `None`.
    """
    try:
        data = json.loads(answer)
    except (ValueError, RecursionError):
        # RecursionError — ответ из тысяч вложенных скобок: это тоже «не разобрать».
        return None
    if not isinstance(data, dict):
        return None
    if not template.free_form:
        return [
            {"title": item.title, "value": _value(data.get(item.name))} for item in template.fields
        ]
    items = data.get("items")
    if not isinstance(items, list):
        return None
    fields = []
    for item in items[:free_form_max_items]:
        if isinstance(item, dict) and (title := _value(item.get("title"))) is not None:
            fields.append({"title": title, "value": _value(item.get("value"))})
    return fields


def document_text(pages: Sequence[str], paged: bool) -> str:
    """Текст документа для модели: у документа со страницами — с их номерами."""
    if not paged:
        return "\n\n".join(pages).strip()
    return "\n\n".join(f"[стр. {number}]\n{text}" for number, text in enumerate(pages, 1)).strip()


class DocparseService:
    """Запуск разбора и его поток событий (§6.4)."""

    def __init__(
        self,
        uow_factory: DialogUnitOfWorkFactory,
        model: ChatModel,
        recognizer: PageRecognizer,
        estimator: TokenEstimator,
        storage: FileStorage,
        reader: DocumentReader,
        sessions: SessionAuthenticator,
        clock: Clock,
        settings: Settings,
        max_model_len: int,
    ) -> None:
        """Получить зависимости явно; `max_model_len` — контекст модели из окружения."""
        self._uow_factory = uow_factory
        self._model = model
        self._recognizer = recognizer
        self._estimator = estimator
        self._storage = storage
        self._reader = reader
        self._sessions = sessions
        self._clock = clock
        self._settings = settings.docparse
        self._recheck_seconds = settings.auth.session.stream_recheck_seconds
        # Текст документа целиком входит в каждый запрос: реквизиты, содержание, вопросы.
        self._budget = (
            max_model_len
            - self._settings.max_output_tokens
            - settings.llm.safety_margin_tokens
            - self._settings.dialog_reserve_tokens
        )
        self._tasks: set[asyncio.Task[None]] = set()
        self._active: set[UUID] = set()

    def is_forming(self, dialog_id: UUID) -> bool:
        """Идёт ли сейчас разбор этого диалога; процесс один, других источников нет."""
        return dialog_id in self._active

    async def shutdown(self) -> None:
        """Прервать идущие разборы при остановке процесса."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- приём: всё, что отвечает обычной ошибкой до открытия потока ---

    async def start(
        self,
        user: CurrentUser,
        session_id: UUID,
        template_id: str,
        file_name: str,
        chunks: AsyncIterator[bytes],
    ) -> EventChannel:
        """Принять файл, проверить его и начать разбор."""
        template = self._settings.template(template_id)
        if template is None:
            raise validation_error([field_error("template_id", "unknown_value")])
        limit = self._settings.document_max_bytes
        try:
            stored = await self._storage.save("docparse", chunks, limit)
        except FileTooLargeError as error:
            raise file_too_large(limit) from error
        active: UUID | None = None
        try:
            media_type, page_count, pages = await asyncio.to_thread(
                self._inspect, self._storage.path(stored.key), file_name
            )
            now = self._clock.now()
            dialog = Dialog(uuid4(), user.id, "docparse", None, now, now)
            name = display_file_name(file_name)
            async with self._uow_factory() as uow:
                await uow.dialogs.add_dialog(dialog)
                await uow.dialogs.add_docparse(
                    Docparse(
                        dialog_id=dialog.id,
                        status="processing",
                        summary_status="streaming",
                        template_id=template.id,
                        template_title=template.title,
                        free_form=template.free_form,
                        file_name=name,
                        media_type=media_type,
                        storage_key=stored.key,
                        size_bytes=stored.size_bytes,
                        page_count=page_count,
                        fields=[],
                        summary="",
                        document_text="",
                        created_at=now,
                    )
                )
                # Отметка «разбор идёт» — до фиксации, как у ответа в диалоге.
                self._active.add(dialog.id)
                active = dialog.id
        except BaseException:
            if active is not None:
                self._active.discard(active)
            await self._storage.delete(stored.key)
            raise
        run = _Run(
            user.id,
            session_id,
            dialog.id,
            template,
            name,
            media_type,
            stored.key,
            page_count,
            pages,
        )
        task = asyncio.create_task(self._run(run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run.channel

    def _inspect(
        self, path: Path, file_name: str
    ) -> tuple[MediaType, int | None, list[str | None]]:
        """Тип, число страниц и текст, доступный без модели; работа для пула потоков.

        Отказ даётся как можно раньше: по числу страниц и по объёму уже известного текста.
        """
        media_type = self._reader.detect(path, file_name)
        if media_type not in _MEDIA_TYPES:
            raise unsupported_file_type()
        try:
            page_count = self._reader.page_count(path, media_type)
            if page_count is not None and page_count > self._settings.max_pages:
                raise too_many_pages(self._settings.max_pages)
            pages: list[str | None] = []
            known = 0
            for number in range(1, (page_count or 1) + 1):
                text = self._reader.page_text(path, media_type, number)
                pages.append(text)
                known += self._estimator.text(text or "")
                if known > self._budget:
                    raise errors.document_too_long()
        except UnreadableDocumentError as error:
            raise file_unreadable() from error
        return media_type, page_count, pages

    # --- поток: после статуса 200 ошибка возможна только событием ---

    async def _run(self, run: _Run) -> None:
        """Выполнить разбор и завершить поток — что бы ни случилось."""
        channel = run.channel
        watch = StreamWatch(
            channel,
            self._sessions,
            run.session_id,
            lambda: self._exists(run),
            self._settings.run_timeout_seconds,
            self._recheck_seconds,
        )
        try:
            status = await self._process(run, watch)
            channel.emit("done", {"status": status})
        except _RunStoppedError:
            pass
        except _RunFailedError as failure:
            self._emit_error(run, failure.code, failure.page)
        except Exception as error:
            # Текст исключения в журнал не идёт: в нём может оказаться содержимое документа.
            logger.error(
                "docparse failed",
                extra={"error_type": type(error).__name__, "trace": code_locations(error)},
            )
            self._emit_error(run, "internal_error", None)
        finally:
            watch.close()
            try:
                if not run.published:
                    await self._discard(run)
            finally:
                self._active.discard(run.dialog_id)
                channel.finish()

    def _emit_error(self, run: _Run, code: str, page: int | None) -> None:
        data: dict[str, Any] = {"code": code, "message": _ERROR_MESSAGES[code]}
        if page is not None and run.page_count is not None:
            data["details"] = {"page": page, "pages_total": run.page_count}
        run.channel.emit("error", data)

    async def _exists(self, run: _Run) -> bool:
        async with self._uow_factory() as uow:
            return await uow.dialogs.docparse_exists(run.owner_id, run.dialog_id)

    async def _discard(self, run: _Run) -> None:
        """До таблицы реквизитов ничего не сохраняется: скрытый диалог и файл удаляются."""
        try:
            async with self._uow_factory() as uow:
                await uow.dialogs.delete_hidden_docparse(run.owner_id, run.dialog_id)
            await self._storage.delete(run.storage_key)
        except Exception as error:
            # Задача разбора завершается здесь: сбой не должен уйти из неё необработанным.
            logger.error(
                "hidden docparse not discarded",
                extra={"error_type": type(error).__name__, "trace": code_locations(error)},
            )

    @staticmethod
    def _interrupt(reason: Interruption, page: int | None = None) -> Exception:
        return _RunStoppedError() if reason == "stopped" else _RunFailedError(reason, page)

    async def _process(self, run: _Run, watch: StreamWatch) -> str:
        """Шаги разбора по порядку; вернуть итог краткого содержания для события `done`."""
        pages = await self._read_pages(run, watch)
        text = document_text(pages, paged=run.page_count is not None)
        if not any(page.strip() for page in pages):
            # Страницы прочитаны, но текста нет ни в слое, ни после распознавания:
            # спрашивать у модели реквизиты не из чего.
            raise _RunFailedError("recognition_failed")
        run.channel.emit("extraction_started", {})
        fields = await self._extract(run, text, watch)
        async with self._uow_factory() as uow:
            await uow.dialogs.publish_docparse(
                run.owner_id,
                run.dialog_id,
                fields=fields,
                document_text=text,
                title=run.file_name,
                now=self._clock.now(),
            )
        run.published = True
        run.channel.emit("extraction", {"dialog_id": str(run.dialog_id), "fields": fields})
        return await self._summarize(run, text, fields, watch)

    async def _read_pages(self, run: _Run, watch: StreamWatch) -> list[str]:
        """Текст всех страниц: сканы распознаются пачками по 8, по запросу на страницу."""
        pages = list(run.pages)
        if run.page_count is None:
            return [page or "" for page in pages]
        path = self._storage.path(run.storage_key)
        total = run.page_count
        for first in range(1, total + 1, MAX_IMAGES_PER_REQUEST):
            last = min(first + MAX_IMAGES_PER_REQUEST - 1, total)
            run.channel.emit(
                "progress", {"page_from": first, "page_to": last, "pages_total": total}
            )
            scans = [number for number in range(first, last + 1) if pages[number - 1] is None]
            if scans:
                images = [
                    await asyncio.to_thread(self._reader.page_image, path, run.media_type, number)
                    for number in scans
                ]
                try:
                    texts, reason = await watch.result(self._recognizer.recognize(images))
                except RecognitionFailedError as error:
                    # Сбой модели — не свойство документа: код говорит, что с моделью.
                    code = (
                        "model_overloaded" if error.cause == "overloaded" else "model_unavailable"
                    )
                    raise _RunFailedError(code, first) from None
                if texts is None:
                    raise self._interrupt(reason or "stopped", first)
                for number, recognized in zip(scans, texts, strict=True):
                    pages[number - 1] = recognized.strip()
            # Распознанное вместе с уже прочитанным не должно превысить бюджет документа:
            # оставшиеся страницы тогда не распознаются.
            read = sum(self._estimator.text(page or "") for page in pages[:last])
            if read > self._budget:
                raise _RunFailedError("document_too_long", first)
        return [page or "" for page in pages]

    def _extraction_request(self, run: _Run, text: str) -> ChatRequest:
        template = run.template
        if template.free_form:
            system = self._settings.free_form_system_prompt.strip()
        else:
            names = "\n".join(
                f"- {item.name} — {item.title}"
                + (f" ({item.description})" if item.description else "")
                for item in template.fields
            )
            system = f"{self._settings.extraction_system_prompt.strip()}\n\nРеквизиты:\n{names}"
        return ChatRequest(
            [
                ModelMessage("system", [TextPart(system)]),
                ModelMessage("user", [TextPart(document_block(run.file_name, text))]),
            ],
            "low",
            self._settings.max_output_tokens,
            template_schema(template),
        )

    async def _extract(self, run: _Run, text: str, watch: StreamWatch) -> list[dict[str, Any]]:
        """Один запрос к модели со всем текстом документа и схемой шаблона."""
        try:
            answer, reason = await watch.result(
                self._model.complete(self._extraction_request(run, text))
            )
        except ModelOverloadedError:
            raise _RunFailedError("model_overloaded") from None
        except ModelUnavailableError:
            raise _RunFailedError("model_unavailable") from None
        except ContextOverflowError:
            raise _RunFailedError("document_too_long") from None
        if answer is None:
            raise self._interrupt(reason or "stopped")
        fields = extracted_fields(run.template, answer, self._settings.free_form_max_items)
        if fields is None:
            # Модель вернула не то, что требовала схема: таблицу из этого не собрать.
            raise _RunFailedError("model_unavailable")
        return fields

    async def _summarize(
        self, run: _Run, text: str, fields: Sequence[Mapping[str, Any]], watch: StreamWatch
    ) -> str:
        """Краткое содержание потоком; в базу оно пишется один раз — с итоговым состоянием."""
        request = ChatRequest(
            [
                ModelMessage("system", [TextPart(self._settings.summary_system_prompt.strip())]),
                ModelMessage(
                    "user",
                    [TextPart(f"{document_block(run.file_name, text)}\n\n{fields_block(fields)}")],
                ),
            ],
            self._settings.reasoning_effort,
            self._settings.max_output_tokens,
        )
        summary = ""
        status: SummaryStatus = "error"
        failure: Exception | None = None
        try:
            async with aclosing(watched_stream(self._model, request, watch)) as events:
                async for event in events:
                    if isinstance(event, str):
                        status = "stopped" if event == "stopped" else "error"
                        failure = self._interrupt(event)
                    elif isinstance(event, Finished):
                        status = "length_limit" if event.reason == "length" else "complete"
                    elif isinstance(event, ContentDelta):
                        summary += event.text
                        run.channel.emit("delta", {"text": event.text})
        except ModelOverloadedError:
            failure = _RunFailedError("model_overloaded")
        except (ModelUnavailableError, ContextOverflowError):
            failure = _RunFailedError("model_unavailable")
        if not await self._save_summary(run, summary, status):
            # Содержание осталось «пишется»; первое обращение к разбору переведёт его в ошибку.
            raise _RunFailedError("internal_error")
        if failure is not None:
            raise failure
        return status

    async def _save_summary(self, run: _Run, summary: str, status: SummaryStatus) -> bool:
        """Записать краткое содержание; при сбое базы — несколько попыток, как у ответа."""
        for attempt in range(_SAVE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_SAVE_RETRY_SECONDS * attempt)
            try:
                async with self._uow_factory() as uow:
                    # Если разбор удалили, пока писалось содержание, сохранять нечего.
                    await uow.dialogs.finish_summary(run.owner_id, run.dialog_id, summary, status)
                return True
            except Exception as error:
                logger.error(
                    "summary not saved",
                    extra={"error_type": type(error).__name__, "trace": code_locations(error)},
                )
        return False
