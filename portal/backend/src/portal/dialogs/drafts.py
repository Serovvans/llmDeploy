"""Черновик пересказа для сжатия и переноса (docs/portal-api.md §5.10.5, §6.5, §12.1).

Черновик ничего не сохраняет: модель пересказывает учитываемые пары, текст уходит клиенту
потоком. Сохраняют только «Применить» (`PUT …/memory/summary`) и создание диалога с
перенесённым. У сотрудника идёт не больше одного черновика: отметка — в памяти процесса,
он один (§13.5).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from portal.core.errors import field_error, not_found, validation_error
from portal.core.events import EventChannel
from portal.core.logging import code_locations
from portal.core.ports import SessionAuthenticator
from portal.core.settings import Settings
from portal.dialogs import errors
from portal.dialogs.context import CARRIED_TAG, SUMMARY_TAG, data_block, fields_block
from portal.dialogs.domain import Attachment, Dialog, Docparse, Message
from portal.dialogs.generation import ERROR_MESSAGES
from portal.dialogs.memory import compress_range, dialog_pairs
from portal.dialogs.ports import DialogUnitOfWorkFactory
from portal.dialogs.watch import StreamWatch, watched_stream
from portal.llm.ports import (
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

logger = logging.getLogger(__name__)

# Первая строка запроса пересказа — ровно она: по ней запрос узнаёт заглушка (§12.2).
SUMMARY_PROMPT = "Перескажи диалог."
_ALL_POSITIONS = 2**31 - 1

DraftPurpose = Literal["compress", "transfer"]


@dataclass(frozen=True)
class DraftScope:
    """Что пересказывается (§5.10.5).

    `until` — ответ-граница (`None` — пар нет); `messages` — число сообщений диалога до
    границы включительно; `pairs` — пары диапазона в любых режимах; `summary` и `carried` —
    прежний пересказ и перенесённое, если они входят.
    """

    until: Message | None
    messages: int
    pairs: Sequence[tuple[Message, Message]]
    summary: str | None
    carried: str | None


@dataclass(frozen=True)
class DraftInput:
    """Текст запроса пересказа и сколько ранних сообщений диапазона в него не вошло."""

    text: str
    dropped_messages: int


def _covered(messages: Sequence[Message], until: Message | None) -> int:
    return 0 if until is None else sum(item.position <= until.position for item in messages)


def compress_scope(
    messages: Sequence[Message], dialog: Dialog, keep_recent: int
) -> DraftScope | None:
    """Диапазон «Сжать раннюю часть» — тот же, что `Memory.compress_pairs`; `None` — нечего.

    Прежний пересказ входит: повторное сжатие пересказывает его вместе с новыми парами.
    Перенесённое не входит: оно не сжимается.
    """
    pairs = dialog_pairs(messages)
    chosen = compress_range(pairs, dialog.summary_until_position, keep_recent)
    if chosen is None:
        return None
    selected = pairs[chosen.first - 1 : chosen.last]
    until = selected[-1][1]
    return DraftScope(until, _covered(messages, until), selected, dialog.summary, None)


def transfer_scope(
    messages: Sequence[Message], dialog: Dialog, until: Message | None
) -> DraftScope:
    """Диапазон переноса: пары до ответа `until` включительно, без него — до конца (§5.10.5).

    Прежний пересказ входит, если его граница не дальше `until`, — тогда вместо пар до неё;
    иначе пересказываются сами пары. Перенесённое входит всегда.
    """
    pairs = dialog_pairs(messages)
    if until is None and pairs:
        until = pairs[-1][1]
    limit = -1 if until is None else until.position
    boundary = dialog.summary_until_position
    summary = dialog.summary if boundary is not None and boundary <= limit else None
    start = boundary if summary is not None and boundary is not None else -1
    selected = [(asked, answer) for asked, answer in pairs if start < answer.position <= limit]
    return DraftScope(until, _covered(messages, until), selected, summary, dialog.carried_text)


def _attachment_block(attachment: Attachment) -> str:
    """Вложение в пересказе: текст или расшифровка; картинки модели не передаются (§12.1)."""
    parts = [attachment.text_content] if attachment.text_content else []
    if scans := len(attachment.image_pages):
        parts.append(f"(картинок: {scans} — в пересказ не передаются)")
    return data_block(
        "вложение", "\n\n".join(parts) or "(в файле нет текста)", attachment.file_name
    )


def _pair_blocks(
    asked: Message, answer: Message, attachments: Mapping[UUID, Sequence[Attachment]]
) -> list[str]:
    files = [] if asked.memory_mode == "text_only" else attachments.get(asked.id, [])
    question = "\n\n".join([*(_attachment_block(item) for item in files), asked.content]).strip()
    blocks = [data_block("пользователь", question)] if question else []
    if answer.content:
        blocks.append(data_block("помощник", answer.content))
    return blocks


def draft_input(
    scope: DraftScope,
    attachments: Mapping[UUID, Sequence[Attachment]],
    docparse: Docparse | None,
    system: str,
    budget: int,
    estimator: TokenEstimator,
) -> DraftInput | None:
    """Сообщение пользователя запроса пересказа, уложенное в бюджет `S` (§5.10.5, §12.1).

    Пары «Не учитывать» не входят, у пар «Без вложений» нет вложений, пустой ответ
    опускается. Не помещается — ранние пары диапазона модели не передаются; `None` — не
    помещается ни одна пара непустого диапазона или даже то, что входит всегда.
    """
    fixed = [SUMMARY_PROMPT]
    if scope.carried is not None:
        fixed.append(data_block(CARRIED_TAG, scope.carried))
    if scope.summary is not None:
        fixed.append(data_block(SUMMARY_TAG, scope.summary))
    if docparse is not None:
        fixed.append(fields_block(docparse.fields))
        if docparse.summary:
            fixed.append(data_block("краткое_содержание", docparse.summary))
    pairs = [
        blocks
        for asked, answer in scope.pairs
        if asked.memory_mode != "excluded" and (blocks := _pair_blocks(asked, answer, attachments))
    ]
    room = (
        budget
        - estimator.text(system)
        - estimator.text("\n".join(fixed))
        - estimator.text(data_block("диалог", ""))
    )
    costs = [sum(estimator.text(block) for block in blocks) for blocks in pairs]
    total, skipped, dropped = sum(costs), 0, 0
    while skipped < len(pairs) and total > room:
        total -= costs[skipped]
        dropped += len(pairs[skipped])
        skipped += 1
    if total > room or (pairs and skipped == len(pairs)):
        return None
    kept = [block for blocks in pairs[skipped:] for block in blocks]
    if kept:
        fixed.append(data_block("диалог", "\n".join(kept)))
    return DraftInput("\n".join(fixed), dropped)


@dataclass(frozen=True)
class _Draft:
    owner_id: UUID
    session_id: UUID
    dialog_id: UUID
    request: ChatRequest
    channel: EventChannel


class SummaryDrafts:
    """Черновики пересказа: проверки до потока, поток, отметка «идёт черновик»."""

    def __init__(
        self,
        uow_factory: DialogUnitOfWorkFactory,
        model: ChatModel,
        estimator: TokenEstimator,
        sessions: SessionAuthenticator,
        wait_idle: Callable[[UUID, UUID], Awaitable[None]],
        settings: Settings,
        max_model_len: int,
    ) -> None:
        """Получить зависимости явно.

        `wait_idle` ждёт, что в диалоге не формируется ответ, иначе отвечает `409` — та же
        проверка, что у смены режима пары (§5.10.4).
        """
        self._uow_factory = uow_factory
        self._model = model
        self._estimator = estimator
        self._sessions = sessions
        self._wait_idle = wait_idle
        self._memory = settings.memory
        self._timeout_seconds = settings.dialogs.generation_timeout_seconds
        self._recheck_seconds = settings.auth.session.stream_recheck_seconds
        self._budget = (
            max_model_len - settings.memory.summary_max_tokens - settings.llm.safety_margin_tokens
        )
        self._drafting: set[UUID] = set()  # сотрудники, у которых идёт черновик
        self._tasks: set[asyncio.Task[None]] = set()

    async def shutdown(self) -> None:
        """Прервать идущие черновики при остановке процесса: они ничего не сохраняют."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def start(
        self,
        owner_id: UUID,
        session_id: UUID,
        dialog_id: UUID,
        purpose: DraftPurpose,
        until_message_id: UUID | None,
    ) -> EventChannel:
        """Проверить всё, что отвечает обычной ошибкой, и начать черновик (§5.10.5).

        Порядок: у сжатия — занятость (`409`); граница переноса (`422`); нечего
        пересказывать (`409`); вход не помещается (`422`); уже идёт черновик (`429`).
        """
        if purpose == "compress":
            # Пересказ ранней части готовится, когда диалог стоит; переносу ответ не мешает:
            # идущий ответ ещё пуст и в пересказ не входит (§5.10.8).
            await self._wait_idle(owner_id, dialog_id)
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
            if dialog is None:
                raise not_found()
            messages = await uow.dialogs.history(owner_id, dialog_id, _ALL_POSITIONS)
            until = None
            if until_message_id is not None:
                until = next((item for item in messages if item.id == until_message_id), None)
                if until is None or until.role != "assistant":
                    raise validation_error([field_error("until_message_id", "unknown_value")])
            scope = (
                compress_scope(messages, dialog, self._memory.keep_recent_pairs)
                if purpose == "compress"
                else transfer_scope(messages, dialog, until)
            )
            docparse = None
            if dialog.kind == "docparse":
                docparse = await uow.dialogs.get_docparse(owner_id, dialog_id)
            if scope is None or not _has_content(scope, docparse):
                raise errors.nothing_to_summarize()
            files = await uow.dialogs.message_attachments(
                owner_id, [asked.id for asked, _ in scope.pairs]
            )
        system = self._memory.summary_system_prompt.strip()
        built = draft_input(scope, files, docparse, system, self._budget, self._estimator)
        if built is None:
            raise errors.message_too_long()
        request = ChatRequest(
            [
                ModelMessage("system", [TextPart(system)]),
                ModelMessage("user", [TextPart(built.text)]),
            ],
            "low",
            self._memory.summary_max_tokens,
        )
        # Проверка и установка отметки — один шаг без ожидания между ними: из двух
        # одновременных запросов проходит ровно один. Всё, что обращается к базе, — раньше.
        if owner_id in self._drafting:
            raise errors.draft_in_progress()
        self._drafting.add(owner_id)
        draft = _Draft(owner_id, session_id, dialog_id, request, EventChannel())
        draft.channel.emit(
            "start",
            {
                "until_message_id": None if scope.until is None else str(scope.until.id),
                "messages": scope.messages,
                "dropped_messages": built.dropped_messages,
            },
        )
        task = asyncio.create_task(self._run(draft))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return draft.channel

    async def _run(self, draft: _Draft) -> None:
        """Передать черновик клиенту и снять отметку — что бы ни случилось."""
        code: str | None = None
        try:
            code = await self._produce(draft)
        except Exception as error:
            logger.error(
                "summary draft failed",
                extra={"error_type": type(error).__name__, "trace": code_locations(error)},
            )
            code = "internal_error"
        finally:
            self._drafting.discard(draft.owner_id)
            if code is not None:
                draft.channel.emit("error", {"code": code, "message": ERROR_MESSAGES[code]})
            draft.channel.finish()

    async def _produce(self, draft: _Draft) -> str | None:
        """Читать поток модели; вернуть код ошибки или `None` — `done` отправлен либо остановка.

        Текст длиннее `memory.max_chars` обрезается по пределу, запрос к модели при этом
        отменяется. Размышления клиенту не отправляются.
        """
        channel = draft.channel
        watch = StreamWatch(
            channel,
            self._sessions,
            draft.session_id,
            lambda: self._dialog_exists(draft),
            self._timeout_seconds,
            self._recheck_seconds,
        )
        sent = 0
        try:
            async with aclosing(watched_stream(self._model, draft.request, watch)) as events:
                async for event in events:
                    if isinstance(event, str):
                        return None if event == "stopped" else event
                    if isinstance(event, Finished):
                        channel.emit("done", {"truncated": event.reason == "length"})
                        return None
                    if isinstance(event, ContentDelta):
                        room = self._memory.max_chars - sent
                        if piece := event.text[:room]:
                            channel.emit("delta", {"text": piece})
                            sent += len(piece)
                        if len(event.text) > room:
                            channel.emit("done", {"truncated": True})
                            return None
        except ModelOverloadedError:
            return "model_overloaded"
        except ModelUnavailableError:
            return "model_unavailable"
        except ContextOverflowError:
            return "message_too_long"
        finally:
            watch.close()
        return "model_unavailable"  # поток без `Finished` полным не считается

    async def _dialog_exists(self, draft: _Draft) -> bool:
        async with self._uow_factory() as uow:
            return await uow.dialogs.get_dialog(draft.owner_id, draft.dialog_id) is not None


def _has_content(scope: DraftScope, docparse: Docparse | None) -> bool:
    """Есть ли что пересказывать: учитываемая пара, прежний пересказ, перенесённое, разбор."""
    considered = any(asked.memory_mode != "excluded" for asked, _ in scope.pairs)
    return (
        considered or scope.summary is not None or scope.carried is not None or docparse is not None
    )
