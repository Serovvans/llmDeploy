"""Формирование ответа: проверки, сохранение, поток событий, остановка, название.

Ответ формируется в собственной задаче, не привязанной к соединению клиента
(docs/portal-api.md §5.5): закрытие соединения — это остановка, и полученная часть
сохраняется; удаление сессии обрывает ответ; название предлагается параллельно (§5.7).
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from portal.core.errors import field_error, not_found, validation_error
from portal.core.events import EventChannel
from portal.core.logging import code_locations
from portal.core.ports import Clock, CurrentUser, SessionAuthenticator
from portal.core.settings import Settings
from portal.dialogs import errors
from portal.dialogs.context import (
    ImageRef,
    Turn,
    fit_history,
    fits_alone,
    history_turns,
    question_turn,
)
from portal.dialogs.domain import AnswerMode, Knowledge, Message, MessageStatus
from portal.dialogs.footnotes import cited_numbers
from portal.dialogs.ports import DialogUnitOfWork, DialogUnitOfWorkFactory
from portal.files.ports import DocumentReader, FileStorage
from portal.kb.ports import KnowledgeBase, KnowledgeUnavailableError, Retrieval, Source
from portal.llm.ports import (
    MAX_IMAGES_PER_REQUEST,
    ChatModel,
    ChatRequest,
    ContentDelta,
    ContextOverflowError,
    Finished,
    ImagePart,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    ReasoningDelta,
    ReasoningEffort,
    TextPart,
    TokenEstimator,
)

logger = logging.getLogger(__name__)

_TITLE_QUESTION_CHARS = 2000
_TITLE_MAX_LENGTH = 80
_TITLE_QUOTES = "\"'«»“”„"
_RETRY_PAUSE_SECONDS = 0.1
_SAVE_ATTEMPTS = 4
_SAVE_RETRY_SECONDS = 0.5
_EFFORT: dict[AnswerMode, ReasoningEffort] = {"fast": "low", "thorough": "xhigh"}
_ERROR_MESSAGES = {
    "model_unavailable": "Модель сейчас недоступна. Повторите попытку позже.",
    "model_overloaded": "Модель перегружена. Повторите попытку позже.",
    "message_too_long": "Сообщение слишком длинное. Сократите текст или вложения.",
    "knowledge_unavailable": "База знаний сейчас недоступна.",
    "generation_timeout": "Ответ формировался слишком долго и был прерван.",
    "session_ended": "Сеанс завершён. Войдите снова.",
    "dialog_deleted": "Диалог удалён.",
    "internal_error": "Внутренняя ошибка. Повторите попытку позже.",
}


@dataclass(frozen=True)
class ChatMessageInput:
    """Тело запроса сообщения в чате (§5.3)."""

    content: str
    attachment_ids: Sequence[UUID]
    mode: AnswerMode
    knowledge: Knowledge


@dataclass
class _Outcome:
    """Итог ответа: то, что будет сохранено."""

    status: MessageStatus = "error"
    error_code: str | None = "internal_error"
    content: str = ""
    reasoning: str = ""
    reasoning_seconds: int | None = None
    dropped_messages: int = 0
    found: Sequence[Source] | None = None  # None — поиск в базе знаний не выполнялся


@dataclass
class _Watch:
    """За чем следят, пока формируется ответ: соединение клиента, срок, сессия, диалог."""

    gone: asyncio.Task[bool]
    started: float
    deadline: float
    next_check: float


@dataclass
class _Job:
    """Один формируемый ответ."""

    owner_id: UUID
    session_id: UUID
    dialog_id: UUID
    question: Message
    question_turn: Turn
    answer_id: UUID
    needs_title: bool
    channel: EventChannel = field(default_factory=EventChannel)
    outcome: _Outcome = field(default_factory=_Outcome)
    leftover_files: list[str] = field(default_factory=list)


# Проверки и сохранение вопроса под блокировкой диалога; получает последнее сообщение.
type _Save = Callable[[DialogUnitOfWork, Message | None], Awaitable[_Job]]


def clean_title(raw: str) -> str:
    """Название из ответа модели: первая непустая строка без кавычек, до 80 символов."""
    for line in raw.splitlines():
        title = line.strip().strip(_TITLE_QUOTES).strip()
        if title:
            return title[:_TITLE_MAX_LENGTH].rstrip()
    return ""


class GenerationService:
    """Отправка вопроса, повторная генерация и поток ответа."""

    def __init__(
        self,
        uow_factory: DialogUnitOfWorkFactory,
        model: ChatModel,
        estimator: TokenEstimator,
        storage: FileStorage,
        reader: DocumentReader,
        sessions: SessionAuthenticator,
        knowledge: KnowledgeBase,
        clock: Clock,
        settings: Settings,
        max_model_len: int,
    ) -> None:
        """Получить зависимости явно; `max_model_len` — контекст модели из окружения."""
        self._knowledge = knowledge
        self._knowledge_reserve = settings.kb.context_max_tokens
        self._uow_factory = uow_factory
        self._model = model
        self._estimator = estimator
        self._storage = storage
        self._reader = reader
        self._sessions = sessions
        self._clock = clock
        self._chat = settings.chat
        self._dialogs = settings.dialogs
        self._llm = settings.llm
        self._recheck_seconds = settings.auth.session.stream_recheck_seconds
        self._max_model_len = max_model_len
        self._tasks: set[asyncio.Task[None]] = set()
        self._active: set[UUID] = set()  # ответы, которые сейчас формируются

    def is_forming(self, answer_id: UUID) -> bool:
        """Формируется ли этот ответ сейчас; процесс один, других источников нет."""
        return answer_id in self._active

    async def shutdown(self) -> None:
        """Прервать идущие ответы при остановке процесса; при старте они станут `interrupted`."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # --- приём запроса: всё, что отвечает обычной ошибкой до открытия потока ---

    async def send(
        self, user: CurrentUser, session_id: UUID, dialog_id: UUID, body: ChatMessageInput
    ) -> EventChannel:
        """Сохранить вопрос с пустым ответом и начать формировать ответ."""
        self._check_input(body)

        async def save(uow: DialogUnitOfWork, last: Message | None) -> _Job:
            files = await uow.dialogs.dialog_attachments(user.id, dialog_id)
            unsent = {item.id: item for item in files if item.message_id is None}
            if len(set(body.attachment_ids)) != len(body.attachment_ids) or any(
                attachment_id not in unsent for attachment_id in body.attachment_ids
            ):
                raise validation_error([field_error("attachment_ids", "invalid_format")])
            # Порядок вложений в сообщении — порядок загрузки: он же будет при чтении
            # истории, отдельного поля порядка в таблице нет.
            chosen = set(body.attachment_ids)
            attachments = [item for item in files if item.id in chosen]
            if sum(item.image_count for item in attachments) > MAX_IMAGES_PER_REQUEST:
                raise errors.too_many_images()
            turn = question_turn(body.content, attachments)
            params = {"mode": body.mode, "knowledge": body.knowledge}
            self._check_fits(turn, body.content, params)

            question = self._message(
                dialog_id, last.position + 1 if last else 0, "user", "complete", params
            )
            question.content = body.content
            await uow.dialogs.add_message(question)
            await uow.dialogs.attach(user.id, body.attachment_ids, question.id)
            # Неотправленные вложения, не вошедшие в вопрос, — остатки прежнего сеанса
            # страницы: строки удаляются здесь, файлы — после фиксации (§5.8).
            leftovers = [item for item in unsent.values() if item.id not in chosen]
            for item in leftovers:
                await uow.dialogs.delete_attachment(user.id, item.id)
            job = _Job(user.id, session_id, dialog_id, question, turn, uuid4(), False)
            job.leftover_files = [item.storage_key for item in leftovers]
            return job

        return await self._start(user.id, dialog_id, save)

    async def regenerate(
        self, user: CurrentUser, session_id: UUID, dialog_id: UUID
    ) -> EventChannel:
        """Заменить последний ответ диалога новым — на тот же вопрос с теми же параметрами."""

        async def save(uow: DialogUnitOfWork, last: Message | None) -> _Job:
            if last is None:
                raise errors.nothing_to_regenerate()
            question = last
            if last.role == "assistant":
                earlier = await uow.dialogs.history(user.id, dialog_id, last.position)
                if not earlier or earlier[-1].role != "user":
                    raise errors.nothing_to_regenerate()
                question = earlier[-1]
                await uow.dialogs.delete_message(user.id, last.id)
            files = await uow.dialogs.message_attachments(user.id, [question.id])
            turn = question_turn(question.content, files.get(question.id, []))
            self._check_fits(turn, question.content, question.params)
            return _Job(user.id, session_id, dialog_id, question, turn, uuid4(), False)

        return await self._start(user.id, dialog_id, save)

    def _check_input(self, body: ChatMessageInput) -> None:
        if len(body.content) > self._dialogs.message_max_chars:
            raise validation_error([field_error("content", "too_long")])
        if len(body.attachment_ids) > self._chat.max_attachments:
            raise validation_error([field_error("attachment_ids", "too_long")])
        if not body.content.strip() and not body.attachment_ids:
            raise validation_error([field_error("content", "required")])

    def _budget(self, mode: AnswerMode) -> int:
        """Сколько токенов остаётся запросу: контекст минус ответ и запас (§5.4)."""
        output = getattr(self._chat.max_output_tokens, mode)
        return self._max_model_len - int(output) - self._llm.safety_margin_tokens

    def _check_fits(self, turn: Turn, content: str, params: Mapping[str, Any]) -> None:
        """Вопрос обязан помещаться сам; под найденное в базе знаний оставлен резерв.

        Резерв `kb.context_max_tokens` вычитается, когда поиск будет выполняться: тогда
        правила и найденные места заведомо не вытеснят сам вопрос (§5.4).
        """
        budget = self._budget(params["mode"])
        if _searches(content, params):
            budget -= self._knowledge_reserve
        if not fits_alone(self._chat.system_prompt, turn, budget, self._estimator):
            raise errors.message_too_long()

    def _message(
        self,
        dialog_id: UUID,
        position: int,
        role: Any,
        status: MessageStatus,
        params: dict[str, Any],
    ) -> Message:
        return Message(
            id=uuid4(),
            dialog_id=dialog_id,
            position=position,
            role=role,
            content="",
            status=status,
            error_code=None,
            reasoning=None,
            reasoning_seconds=None,
            params=params,
            sources=None,
            sources_found=None,
            dropped_messages=0,
            created_at=self._clock.now(),
        )

    async def _start(self, owner_id: UUID, dialog_id: UUID, save: _Save) -> EventChannel:
        """Под блокировкой строки диалога проверить, что ответ не формируется, и сохранить.

        Если последний ответ ещё `streaming`, транзакция закрывается и проверка
        повторяется до `dialogs.stop_grace_seconds`: клиент мог только что закрыть прежний
        поток, и сервер ещё сохраняет остановку (§5.5, «Параллельность»).
        """
        deadline = time.monotonic() + self._dialogs.stop_grace_seconds
        while (job := await self._try_accept(owner_id, dialog_id, save)) is None:
            if time.monotonic() >= deadline:
                raise errors.generation_in_progress()
            await asyncio.sleep(_RETRY_PAUSE_SECONDS)
        # Вопрос зафиксирован в базе: только теперь уйдёт статус 200 и начнётся ответ.
        # От фиксации до старта задачи нет ни одного ожидания.
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.channel

    async def _try_accept(self, owner_id: UUID, dialog_id: UUID, save: _Save) -> _Job | None:
        """Одна попытка принять вопрос; `None` — в диалоге ещё формируется ответ."""
        answer_id: UUID | None = None
        try:
            async with self._uow_factory() as uow:
                dialog = await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True)
                if dialog is None:
                    raise not_found()
                last = await uow.dialogs.last_message(owner_id, dialog_id)
                if last is not None and last.status == "streaming":
                    if last.id in self._active:
                        return None
                    # Процесс один: «формируется» без живой задачи — это ответ, который
                    # не удалось сохранить. Он прерван, диалог не должен оставаться запертым.
                    await uow.dialogs.interrupt_answer(owner_id, last.id)
                    last.status = "error"
                job = await save(uow, last)
                job.needs_title = dialog.title is None
                answer = self._message(
                    dialog_id, job.question.position + 1, "assistant", "streaming", {}
                )
                answer.id = job.answer_id
                await uow.dialogs.add_message(answer)
                await uow.dialogs.touch_dialog(owner_id, dialog_id, self._clock.now())
                # Отметка «формируется» ставится до фиксации, под блокировкой диалога:
                # как только строка ответа станет видна другим запросам, она уже
                # отмечена, и никто не примет её за брошенную.
                answer_id = job.answer_id
                self._active.add(answer_id)
        except BaseException:
            if answer_id is not None:
                self._active.discard(answer_id)  # фиксация не удалась — ответа нет
            raise
        return job

    # --- формирование ответа: после статуса 200 ошибка возможна только событием ---

    async def _run(self, job: _Job) -> None:
        """Сформировать ответ, сохранить его и завершить поток — что бы ни случилось."""
        channel = job.channel
        channel.emit(
            "start",
            {"user_message_id": str(job.question.id), "assistant_message_id": str(job.answer_id)},
        )
        title_task = asyncio.create_task(self._suggest_title(job)) if job.needs_title else None
        try:
            await self._drop_leftovers(job)
            await self._finish(job, title_task)
        finally:
            if title_task is not None:
                title_task.cancel()
            self._active.discard(job.answer_id)
            channel.finish()

    async def _finish(self, job: _Job, title_task: asyncio.Task[None] | None) -> None:
        channel, outcome = job.channel, job.outcome
        try:
            await self._produce(job)
        except Exception as error:
            self._log_failure("generation failed", error)
            outcome.status, outcome.error_code = "error", "internal_error"
        if outcome.error_code != "dialog_deleted" and not await self._save(job):
            # Ответ остался «формируется»; следующая отправка в диалог это исправит.
            outcome.status, outcome.error_code = "error", "internal_error"

        finished = outcome.status in ("complete", "length_limit")
        if title_task is not None:
            if finished:
                await asyncio.wait({title_task}, timeout=self._dialogs.title_wait_seconds)
            title_task.cancel()
            await asyncio.gather(title_task, return_exceptions=True)
        if finished:
            channel.emit("done", {"status": outcome.status})
        elif outcome.status == "error" and outcome.error_code is not None:
            code = outcome.error_code
            channel.emit("error", {"code": code, "message": _ERROR_MESSAGES[code]})

    async def _drop_leftovers(self, job: _Job) -> None:
        """Удалить файлы неотправленных остатков; их строки удалены вместе с вопросом.

        Сбой здесь ответу не мешает: файл без строки никому не виден.
        """
        for key in job.leftover_files:
            try:
                await self._storage.delete(key)
            except OSError as error:
                self._log_failure("leftover file not deleted", error)

    @staticmethod
    def _log_failure(message: str, error: Exception) -> None:
        """Тип исключения и места в коде; текст исключения в журнал не идёт."""
        logger.error(
            message, extra={"error_type": type(error).__name__, "trace": code_locations(error)}
        )

    async def _save(self, job: _Job) -> bool:
        """Сохранить ответ; при сбое базы — несколько попыток. `False` — не удалось."""
        outcome = job.outcome
        cited = None
        if outcome.found is not None:
            # Хранятся только источники, на которые в тексте есть сноска вне кода (§5.5).
            numbers = cited_numbers(outcome.content, [source.n for source in outcome.found])
            cited = [_source_data(source) for source in outcome.found if source.n in numbers]
        for attempt in range(_SAVE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_SAVE_RETRY_SECONDS * attempt)
            try:
                async with self._uow_factory() as uow:
                    await uow.dialogs.finish_answer(
                        job.owner_id,
                        job.answer_id,
                        content=outcome.content,
                        status=outcome.status,
                        error_code=outcome.error_code if outcome.status == "error" else None,
                        reasoning=outcome.reasoning or None,
                        reasoning_seconds=outcome.reasoning_seconds,
                        sources=cited,
                        sources_found=None if outcome.found is None else len(outcome.found),
                        dropped_messages=outcome.dropped_messages,
                    )
                return True
            except Exception as error:
                self._log_failure("answer not saved", error)
        return False

    async def _produce(self, job: _Job) -> None:
        """Собрать запрос и получить ответ; итог записывается в `job.outcome`."""
        started = time.monotonic()
        watch = _Watch(
            gone=asyncio.create_task(job.channel.consumer_gone.wait()),
            started=started,
            deadline=started + self._dialogs.generation_timeout_seconds,
            next_check=started + self._recheck_seconds,
        )
        try:
            await self._answer(job, watch)
        finally:
            watch.gone.cancel()

    async def _answer(self, job: _Job, watch: _Watch) -> None:
        outcome = job.outcome
        mode: AnswerMode = job.question.params["mode"]
        system = self._chat.system_prompt
        question = job.question_turn
        if _searches(job.question.content, job.question.params):
            retrieval = await self._search(job, watch)
            if retrieval is None:
                return
            # Правила — в конец системного сообщения; найденное — первым блоком вопроса,
            # перед вложениями и текстом пользователя. В историю оно не попадает (§5.4).
            system = f"{system}\n\n{retrieval.rules}"
            if retrieval.context:
                question = Turn("user", f"{retrieval.context}\n\n{question.text}", question.images)

        async with self._uow_factory() as uow:
            earlier = await uow.dialogs.history(job.owner_id, job.dialog_id, job.question.position)
            files = await uow.dialogs.message_attachments(
                job.owner_id, [message.id for message in earlier if message.role == "user"]
            )
        history = history_turns(earlier, files)
        budget = self._budget(mode)
        dropped = fit_history(system, history, question, budget, self._estimator)
        if dropped:
            job.channel.emit("context_truncated", {"dropped_messages": dropped})

        for attempt in range(2):
            outcome.dropped_messages = dropped
            request = ChatRequest(
                await self._model_messages(system, [*history[dropped:], question]),
                _EFFORT[mode],
                int(getattr(self._chat.max_output_tokens, mode)),
            )
            try:
                await self._consume(job, request, watch)
                return
            except ContextOverflowError:
                # Оценка числа токенов ошиблась: один повтор с вдвое меньшей историей.
                remaining = len(history) - dropped
                if attempt or not remaining:
                    break
                more = max(dropped * 2, dropped + (remaining + 1) // 2)
                dropped = fit_history(system, history, question, budget, self._estimator, more)
        outcome.status, outcome.error_code = "error", "message_too_long"

    async def _search(self, job: _Job, watch: _Watch) -> Retrieval | None:
        """Найти места в базе знаний; `None` — ответа не будет, итог записан.

        Поиск идёт по тексту вопроса как есть, с фильтром доступа по пользователю и
        выбранной области. Остановка, конец сессии и срок прерывают и его.
        """
        outcome = job.outcome
        job.channel.emit("search_started", {})
        search = asyncio.ensure_future(
            self._knowledge.retrieve(
                job.question.content, job.owner_id, job.question.params["knowledge"]
            )
        )
        try:
            if not await self._wait(job, search, watch):
                return None
            retrieval = search.result()
        except KnowledgeUnavailableError:
            # Ответ без базы знаний сервер не подменяет.
            outcome.status, outcome.error_code = "error", "knowledge_unavailable"
            return None
        finally:
            if not search.done():
                search.cancel()
                await asyncio.gather(search, return_exceptions=True)
        outcome.found = tuple(retrieval.sources)
        job.channel.emit(
            "sources", {"sources": [_source_data(source) for source in retrieval.sources]}
        )
        return retrieval

    async def _model_messages(self, system: str, turns: Sequence[Turn]) -> list[ModelMessage]:
        messages = [ModelMessage("system", [TextPart(system)])]
        for turn in turns:
            parts: list[TextPart | ImagePart] = [TextPart(turn.text)] if turn.text else []
            for image in turn.images:
                parts.append(await asyncio.to_thread(self._load_image, image))
            messages.append(ModelMessage(turn.role, parts))
        return messages

    def _load_image(self, image: ImageRef) -> ImagePart:
        """Изображение-вложение уходит как есть, страница-скан PDF — отрисованной в PNG."""
        path = self._storage.path(image.storage_key)
        if image.media_type == "image/jpeg" or image.media_type == "image/png":
            return ImagePart(path.read_bytes(), image.media_type)
        return ImagePart(self._reader.page_image(path, image.media_type, image.page), "image/png")

    async def _wait(self, job: _Job, pending: asyncio.Future[Any], watch: _Watch) -> bool:
        """Дождаться `pending`; `False` — ответ прерван, итог записан в `job.outcome`.

        Пока идёт ожидание, проверяется, что клиент не закрыл соединение, не вышел предел
        времени ответа, а сессия и сам ответ ещё существуют.
        """
        outcome = job.outcome
        while True:
            wait = max(0.0, min(watch.next_check, watch.deadline) - time.monotonic())
            await asyncio.wait(
                {pending, watch.gone}, timeout=wait, return_when=asyncio.FIRST_COMPLETED
            )
            now = time.monotonic()
            if watch.gone.done():
                outcome.status, outcome.error_code = "stopped", None
                return False
            if now >= watch.deadline:
                outcome.status, outcome.error_code = "error", "generation_timeout"
                return False
            if now >= watch.next_check:
                watch.next_check = now + self._recheck_seconds
                if not await self._sessions.session_exists(job.session_id):
                    outcome.status, outcome.error_code = "error", "session_ended"
                    return False
                if not await self._answer_exists(job):
                    outcome.status, outcome.error_code = "error", "dialog_deleted"
                    return False
            if pending.done():
                return True

    async def _consume(self, job: _Job, request: ChatRequest, watch: _Watch) -> None:
        """Читать поток модели, пока не случится одно из: конец, остановка, сбой, срок."""
        channel, outcome = job.channel, job.outcome
        stream = self._model.stream(request)
        pending: asyncio.Future[Any] | None = None
        try:
            while True:
                pending = asyncio.ensure_future(anext(stream))
                if not await self._wait(job, pending, watch):
                    return
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    # Поток кончился без `Finished`: ответ полным не считается никогда.
                    outcome.status, outcome.error_code = "error", "model_unavailable"
                    return
                except ModelOverloadedError:
                    outcome.status, outcome.error_code = "error", "model_overloaded"
                    return
                except ModelUnavailableError:
                    outcome.status, outcome.error_code = "error", "model_unavailable"
                    return
                if isinstance(event, Finished):
                    outcome.status = "length_limit" if event.reason == "length" else "complete"
                    outcome.error_code = None
                    return
                if isinstance(event, ReasoningDelta):
                    outcome.reasoning += event.text
                    channel.emit("reasoning_delta", {"text": event.text})
                elif isinstance(event, ContentDelta):
                    if outcome.reasoning and outcome.reasoning_seconds is None:
                        outcome.reasoning_seconds = round(time.monotonic() - watch.started)
                    outcome.content += event.text
                    channel.emit("delta", {"text": event.text})
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            # Закрытие генератора отменяет запрос к модели.
            await _close(stream)

    async def _answer_exists(self, job: _Job) -> bool:
        async with self._uow_factory() as uow:
            return await uow.dialogs.message_exists(job.owner_id, job.answer_id)

    # --- название ---

    async def _suggest_title(self, job: _Job) -> None:
        """Попросить модель назвать диалог; сбой ошибкой не считается (§5.7)."""
        try:
            async with self._uow_factory() as uow:
                first = await uow.dialogs.first_question(job.owner_id, job.dialog_id)
            if not first:
                return
            raw = await self._model.complete(
                ChatRequest(
                    [
                        ModelMessage("system", [TextPart(self._dialogs.title_system_prompt)]),
                        ModelMessage("user", [TextPart(first[:_TITLE_QUESTION_CHARS])]),
                    ],
                    "low",
                    self._dialogs.title_max_tokens,
                )
            )
            title = clean_title(raw)
            if not title:
                return
            async with self._uow_factory() as uow:
                saved = await uow.dialogs.set_title_if_empty(job.owner_id, job.dialog_id, title)
            if saved:
                job.channel.emit("title", {"title": title})
        except (ModelUnavailableError, ModelOverloadedError, ContextOverflowError) as error:
            logger.info("title skipped", extra={"error_type": type(error).__name__})


def _searches(content: str, params: Mapping[str, Any]) -> bool:
    """Будет ли поиск в базе знаний: она включена и у вопроса есть текст (§5.4)."""
    return params["knowledge"] != "none" and bool(content.strip())


def _source_data(source: Source) -> dict[str, Any]:
    """Источник в виде объекта `Source` контракта (§5.1)."""
    return {
        "n": source.n,
        "document_id": str(source.document_id),
        "document_title": source.document_title,
        "scope": source.scope,
        "page": source.page,
        "fragment_id": str(source.fragment_id),
        "quote": source.quote,
    }


async def _close(stream: AsyncIterator[Any]) -> None:
    close = getattr(stream, "aclose", None)
    if close is not None:
        await close()
