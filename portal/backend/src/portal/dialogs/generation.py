"""Формирование ответа: проверки, сохранение, поток событий, остановка, название.

Сценарий один для всех видов диалога; чем виды `sql`, `cogis` и `docparse` отличаются от
чата, определяют их правила — порт `DialogTool`.

Ответ формируется в собственной задаче, не привязанной к соединению клиента
(docs/portal-api.md §5.5): закрытие соединения — это остановка, и полученная часть
сохраняется; удаление сессии обрывает ответ; название предлагается параллельно (§5.7).
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from concurrent.futures import Executor
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from typing import Any, Literal
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
    considered_history,
    fit_memory,
    head_turn,
    history_turns,
    question_turn,
    with_head_rules,
)
from portal.dialogs.domain import (
    AnswerMode,
    Attachment,
    DialogKind,
    MemoryMode,
    Message,
    MessageStatus,
)
from portal.dialogs.files import AttachmentFiles
from portal.dialogs.footnotes import cited_numbers
from portal.dialogs.memory import MemoryEstimate, estimate_memory
from portal.dialogs.ports import (
    AnswerEditor,
    AnswerPlan,
    DialogTool,
    DialogUnitOfWork,
    DialogUnitOfWorkFactory,
    RunReport,
    SqlRechecker,
)
from portal.dialogs.service import MessageView, SummaryView
from portal.dialogs.watch import (
    INTERRUPTION_MESSAGES,
    Interruption,
    StreamWatch,
    watched_stream,
)
from portal.files.ports import DocumentReader, FileStorage
from portal.kb.ports import (
    DialogTurn,
    KnowledgeBase,
    KnowledgeSelection,
    KnowledgeUnavailableError,
    Retrieval,
    Source,
)
from portal.kb.tags import TagsError, normalize_tags
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
    Usage,
)

logger = logging.getLogger(__name__)

_TITLE_QUESTION_CHARS = 2000
_TITLE_MAX_LENGTH = 80
_TITLE_QUOTES = "\"'«»“”„"
_RETRY_PAUSE_SECONDS = 0.1
_SAVE_ATTEMPTS = 4
_SAVE_RETRY_SECONDS = 0.5
_ALL_POSITIONS = 2**31 - 1
_EFFORT: dict[AnswerMode, ReasoningEffort] = {"fast": "low", "thorough": "xhigh"}
ERROR_MESSAGES = {
    "model_unavailable": "Модель сейчас недоступна. Повторите попытку позже.",
    "model_overloaded": "Модель перегружена. Повторите попытку позже.",
    "message_too_long": "Сообщение слишком длинное. Сократите текст или вложения.",
    "knowledge_unavailable": "База знаний сейчас недоступна.",
    "internal_error": "Внутренняя ошибка. Повторите попытку позже.",
    **INTERRUPTION_MESSAGES,
}


class _Unset:
    """Значение «не передано»: `None` у схемы SQL — осмысленный выбор «без схемы»."""


UNSET = _Unset()


@dataclass(frozen=True)
class QuestionInput:
    """Вопрос после разбора тела запроса по виду диалога (§5.3).

    `params` — то, что сохраняется вместе с вопросом для повторной генерации.
    """

    content: str
    params: dict[str, Any]
    attachment_ids: Sequence[UUID] = ()


@dataclass(frozen=True)
class MemoryQuery:
    """Что войдёт в план следующего вопроса — строка запроса оценки памяти (§5.10.3).

    `params` — параметры вопроса своего вида, как у `QuestionInput`; `search` — только в
    чате: в выборе базы знаний есть коллекция, и под найденное нужен резерв.
    """

    params: dict[str, Any]
    search: bool = False


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
    sql_check: Mapping[str, Any] | None = None


@dataclass
class _Job:
    """Один формируемый ответ."""

    owner_id: UUID
    session_id: UUID
    dialog_id: UUID
    question: Message
    plan: AnswerPlan
    answer_id: UUID
    tool: DialogTool | None = None
    kind: DialogKind = "chat"
    needs_title: bool = False
    # Опасные операции в только что отправленном вопросе: событие `sql_question_check`.
    announce_dangers: Sequence[str] = ()
    channel: EventChannel = field(default_factory=EventChannel)
    outcome: _Outcome = field(default_factory=_Outcome)
    leftovers: list[Attachment] = field(default_factory=list)


# Проверки и сохранение вопроса под блокировкой диалога; получает последнее сообщение.
type _Save = Callable[[DialogUnitOfWork, Message | None], Awaitable[_Job]]
# Текст ответа для истории своего вида: у `code` блоки правки заменены строкой (§7.5.9).
type HistoryView = Callable[[str], str]


def viewed(messages: Sequence[Message], view: HistoryView | None) -> list[Message]:
    """Сообщения с текстом ответов для истории; сохранённые сообщения не меняются."""
    if view is None:
        return list(messages)
    return [
        replace(message, content=view(message.content)) if message.role == "assistant" else message
        for message in messages
    ]


def search_history(
    messages: Sequence[Message], summarized_until: int | None = None
) -> list[DialogTurn]:
    """История для поиска: только текст учитываемых вопросов и ответов по порядку (§5.4).

    Пересказанных сообщений, пар «Не учитывать», размышлений, блоков вложений и найденных
    мест в ней нет (§5.10.1); сообщение с пустым текстом — вопрос из одних вложений или
    оборванный ответ — пропускается.
    """
    return [
        DialogTurn(message.role, message.content)
        for message, _ in considered_history(messages, summarized_until)
        if message.content
    ]


def clean_title(raw: str) -> str:
    """Название из ответа модели: первая непустая строка без кавычек, до 80 символов."""
    for line in raw.splitlines():
        title = line.strip().strip(_TITLE_QUOTES).strip()
        if title:
            return title[:_TITLE_MAX_LENGTH].rstrip()
    return ""


class GenerationService:
    """Отправка вопроса, повторная генерация и поток ответа — для всех видов диалога."""

    def __init__(
        self,
        uow_factory: DialogUnitOfWorkFactory,
        model: ChatModel,
        estimator: TokenEstimator,
        storage: FileStorage,
        files: AttachmentFiles,
        reader: DocumentReader,
        document_executor: Executor,
        sessions: SessionAuthenticator,
        knowledge: KnowledgeBase,
        tools: Mapping[DialogKind, DialogTool],
        summary_forming: Callable[[UUID], bool],
        clock: Clock,
        settings: Settings,
        max_model_len: int,
        editors: Mapping[DialogKind, AnswerEditor] | None = None,
        history_views: Mapping[DialogKind, HistoryView] | None = None,
        rechecker: SqlRechecker | None = None,
    ) -> None:
        """Получить зависимости явно.

        `tools` — правила видов `sql`, `cogis`, `docparse`, `code`; `editors` — правка по
        сохранённому ответу (`code`); `history_views` — текст ответов в истории своего вида
        (§7.5.9). `summary_forming` отвечает,
        пишется ли сейчас краткое содержание разбора; `max_model_len` — контекст модели;
        `document_executor` — потоки чтения документов, отдельные от общего пула.
        """
        self._knowledge = knowledge
        self._knowledge_reserve = settings.kb.context_max_tokens
        self._tools = tools
        self._editors = editors or {}
        self._history_views = history_views or {}
        self._summary_forming = summary_forming
        self._uow_factory = uow_factory
        self._model = model
        self._estimator = estimator
        self._storage = storage
        self._files = files
        self._reader = reader
        self._document_executor = document_executor
        self._sessions = sessions
        self._clock = clock
        self._chat = settings.chat
        self._tags = settings.kb.tags
        self._dialogs = settings.dialogs
        self._llm = settings.llm
        self._memory = settings.memory
        self._recheck_seconds = settings.auth.session.stream_recheck_seconds
        self._max_model_len = max_model_len
        self._tasks: set[asyncio.Task[None]] = set()
        self._active: set[UUID] = set()  # ответы, которые сейчас формируются
        # Диалоги, в которых идёт «Проверить снова» (1.0, §7.4, А-143).
        self._rechecking: set[UUID] = set()
        self._rechecker = rechecker

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
        self,
        user: CurrentUser,
        session_id: UUID,
        dialog_id: UUID,
        kind: DialogKind,
        body: QuestionInput,
    ) -> EventChannel:
        """Сохранить вопрос с пустым ответом и начать формировать ответ."""
        self._check_input(kind, body)
        if kind == "chat":
            self._check_selection(body.params)
            return await self._start(
                user.id,
                dialog_id,
                kind,
                self._save_chat_question(user, session_id, dialog_id, body),
            )
        tool = self._tools[kind]
        dangers = await tool.accept(user.id, dialog_id, body.content, body.params)
        plan = await tool.plan(user.id, dialog_id, body.content, body.params)
        self._check_fits(plan)

        async def save(uow: DialogUnitOfWork, last: Message | None) -> _Job:
            question = self._message(
                dialog_id, last.position + 1 if last else 0, "user", "complete", body.params
            )
            question.content = body.content
            question.sql_dangers = dangers
            await uow.dialogs.add_message(question)
            return _Job(
                user.id,
                session_id,
                dialog_id,
                question,
                plan,
                uuid4(),
                tool,
                announce_dangers=dangers or (),
            )

        return await self._start(user.id, dialog_id, kind, save)

    def _save_chat_question(
        self, user: CurrentUser, session_id: UUID, dialog_id: UUID, body: QuestionInput
    ) -> _Save:
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
            for item in attachments:
                # Запись идёт в вопрос текстом расшифровки: без него отправлять нечего (§5.3).
                if item.transcription_status not in (None, "ready"):
                    raise errors.attachment_not_ready(item.id)
            plan = self._chat_plan(body.content, body.params, attachments)
            self._check_fits(plan)

            question = self._message(
                dialog_id, last.position + 1 if last else 0, "user", "complete", body.params
            )
            question.content = body.content
            await uow.dialogs.add_message(question)
            await uow.dialogs.attach(user.id, body.attachment_ids, question.id)
            # Неотправленные вложения, не вошедшие в вопрос, — остатки прежнего сеанса
            # страницы: строки удаляются здесь, файлы — после фиксации (§5.8).
            leftovers = [item for item in unsent.values() if item.id not in chosen]
            for item in leftovers:
                await uow.dialogs.delete_attachment(user.id, item.id)
            job = _Job(user.id, session_id, dialog_id, question, plan, uuid4())
            job.leftovers = leftovers
            return job

        return save

    async def regenerate(
        self,
        user: CurrentUser,
        session_id: UUID,
        dialog_id: UUID,
        kind: DialogKind,
        schema_id: UUID | _Unset | None = UNSET,
    ) -> EventChannel:
        """Заменить последний ответ диалога новым — на тот же вопрос с теми же параметрами.

        `schema_id` — замена схемы в сохранённых параметрах вопроса диалога `sql` (§5.6).
        """

        async def save(uow: DialogUnitOfWork, last: Message | None) -> _Job:
            if last is None:
                raise errors.nothing_to_regenerate()
            question = last
            if last.role == "assistant":
                earlier = await uow.dialogs.history(user.id, dialog_id, last.position)
                if not earlier or earlier[-1].role != "user":
                    raise errors.nothing_to_regenerate()
                question = earlier[-1]
            tool = self._tools.get(kind)
            if tool is None:
                files = await uow.dialogs.message_attachments(user.id, [question.id])
                plan = self._chat_plan(
                    question.content, question.params, files.get(question.id, [])
                )
            else:
                if not isinstance(schema_id, _Unset):
                    question.params = {
                        **question.params,
                        "schema_id": None if schema_id is None else str(schema_id),
                    }
                    if self._rechecker is not None:
                        # С телом — текущий выбор диалога вместо выбора вопроса (§7.6.9).
                        question.params = await self._rechecker.with_selection(
                            user.id, dialog_id, question.params, question=True
                        )
                await tool.accept(
                    user.id, dialog_id, question.content, question.params, regenerate=True
                )
                plan = await tool.plan(user.id, dialog_id, question.content, question.params)
                await uow.dialogs.set_question_params(user.id, question.id, question.params)
            self._check_fits(plan)
            if last.role == "assistant":
                await uow.dialogs.delete_message(user.id, last.id)
            return _Job(user.id, session_id, dialog_id, question, plan, uuid4(), tool)

        return await self._start(user.id, dialog_id, kind, save)

    def _check_input(self, kind: DialogKind, body: QuestionInput) -> None:
        if len(body.content) > self._dialogs.message_max_chars:
            raise validation_error([field_error("content", "too_long")])
        if len(body.attachment_ids) > self._chat.max_attachments:
            raise validation_error([field_error("attachment_ids", "too_long")])
        # Пустой вопрос допустим только в чате и только с вложениями (§5.3).
        if not body.content.strip() and not (kind == "chat" and body.attachment_ids):
            raise validation_error([field_error("content", "required")])

    def _check_selection(self, params: Mapping[str, Any]) -> None:
        """Выбор базы знаний в вопросе чата: набор без повторов, теги — только формат (§5.3).

        Существование тега не проверяется: ответ не должен раскрывать, какие теги есть.
        """
        collections: Sequence[str] = params["collections"]
        if len(set(collections)) != len(collections):
            raise validation_error([field_error("collections", "invalid_format")])
        # Правило одно с отбором списка документов: тег, принятый там, годен и здесь.
        try:
            normalize_tags(
                params["tags"],
                max_count=self._tags.max_selected,
                max_length=self._tags.max_length,
            )
        except TagsError as error:
            raise validation_error([field_error("tags", error.code)]) from None

    def _chat_plan(
        self, content: str, params: Mapping[str, Any], attachments: Sequence[Attachment]
    ) -> AnswerPlan:
        mode: AnswerMode = params["mode"]
        # Вопрос без текста и пустой набор коллекций поиск не запускают (§5.4).
        selection = None
        if params["collections"] and content.strip():
            selection = KnowledgeSelection(
                frozenset(params["collections"]), frozenset(params["tags"])
            )
        return AnswerPlan(
            system=self._chat.system_prompt,
            question=question_turn(content, attachments),
            effort=_EFFORT[mode],
            max_tokens=int(getattr(self._chat.max_output_tokens, mode)),
            selection=selection,
        )

    def _budget(self, plan: AnswerPlan) -> int:
        """Сколько токенов остаётся запросу: контекст минус ответ и запас (§5.4)."""
        return self._max_model_len - plan.max_tokens - self._llm.safety_margin_tokens

    def _reserved_budget(self, plan: AnswerPlan, search: bool) -> int:
        """Бюджет за вычетом резерва `kb.context_max_tokens`, если поиск будет (§5.10.2)."""
        return self._budget(plan) - (self._knowledge_reserve if search else 0)

    def _check_fits(self, plan: AnswerPlan, head: Turn | None = None) -> None:
        """Вопрос с головным сообщением обязан помещаться сам; под найденное — резерв.

        Резерв вычитается, когда поиск будет выполняться, при любом значении флага памяти:
        тогда правила и найденные места заведомо не вытеснят сам вопрос (§5.4, §5.10.2).
        """
        budget = self._reserved_budget(plan, plan.selection is not None)
        system = with_head_rules(plan.system, head, self._memory.head_rules_prompt)
        fit = fit_memory(system, [], plan.question, budget, self._estimator, head=head)
        if fit.used > fit.limit:
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
            sql_check=None,
            sql_dangers=None,
            dropped_messages=0,
            created_at=self._clock.now(),
        )

    async def _start(
        self, owner_id: UUID, dialog_id: UUID, kind: DialogKind, save: _Save
    ) -> EventChannel:
        """Под блокировкой строки диалога проверить, что ответ не формируется, и сохранить.

        Если последний ответ ещё `streaming`, транзакция закрывается и проверка
        повторяется до `dialogs.stop_grace_seconds`: клиент мог только что закрыть прежний
        поток, и сервер ещё сохраняет остановку (§5.5, «Параллельность»).
        """
        job = await self._when_idle(lambda: self._try_accept(owner_id, dialog_id, kind, save))
        # Вопрос зафиксирован в базе: только теперь уйдёт статус 200 и начнётся ответ.
        # От фиксации до старта задачи нет ни одного ожидания.
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job.channel

    async def _when_idle[T](self, attempt: Callable[[], Awaitable[T | None]]) -> T:
        """Повторять попытку, пока она отвечает `None` — в диалоге формируется ответ.

        Пауза короткая, срок — `dialogs.stop_grace_seconds`, затем
        `409 generation_in_progress` (§5.5, «Параллельность»).
        """
        deadline = time.monotonic() + self._dialogs.stop_grace_seconds
        while (result := await attempt()) is None:
            if time.monotonic() >= deadline:
                raise errors.generation_in_progress()
            await asyncio.sleep(_RETRY_PAUSE_SECONDS)
        return result

    async def _is_idle(
        self, uow: DialogUnitOfWork, owner_id: UUID, dialog_id: UUID, kind: DialogKind
    ) -> tuple[bool, Message | None]:
        """Под блокировкой строки диалога: свободен ли он; заодно — последнее сообщение."""
        if kind == "docparse" and await self._summary_blocks(uow, owner_id, dialog_id):
            return False, None
        if dialog_id in self._rechecking:
            return False, None  # идёт «Проверить снова»: диалог занят, как при ответе
        last = await uow.dialogs.last_message(owner_id, dialog_id)
        if last is not None and last.id in self._active:
            # Ответ ещё формируется либо уже сохранён, но идёт проверка SQL (§5.5).
            return False, last
        if last is not None and last.status == "streaming":
            # Процесс один: «формируется» без живой задачи — это ответ, который
            # не удалось сохранить. Он прерван, диалог не должен оставаться запертым.
            await uow.dialogs.interrupt_answer(owner_id, last.id)
            last.status = "error"
        return True, last

    async def _try_accept(
        self, owner_id: UUID, dialog_id: UUID, kind: DialogKind, save: _Save
    ) -> _Job | None:
        """Одна попытка принять вопрос; `None` — в диалоге ещё формируется ответ."""
        answer_id: UUID | None = None
        try:
            async with self._uow_factory() as uow:
                dialog = await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True)
                if dialog is None or dialog.kind != kind:
                    raise not_found()
                idle, last = await self._is_idle(uow, owner_id, dialog_id, kind)
                if not idle:
                    return None
                job = await save(uow, last)
                job.kind = kind
                # Пересказ и перенесённое входят в каждый запрос: с ними вопрос тоже
                # обязан поместиться (§5.10.2).
                self._check_fits(job.plan, head_turn(dialog))
                # У разбора название — имя файла, у помощника проекта — имя проекта: модель
                # его не предлагает (§5.7, §7.5.8).
                job.needs_title = dialog.title is None and kind not in ("docparse", "code")
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

    async def _summary_blocks(self, uow: DialogUnitOfWork, owner_id: UUID, dialog_id: UUID) -> bool:
        """Пишется ли краткое содержание разбора: тогда вопрос ждёт, как при ответе (§7.3)."""
        docparse = await uow.dialogs.get_docparse(owner_id, dialog_id)
        if docparse is None or docparse.summary_status != "streaming":
            return False
        if self._summary_forming(dialog_id):
            return True
        # Поток разбора уже не идёт: краткое содержание осталось неполным.
        await uow.dialogs.finish_summary(owner_id, dialog_id, docparse.summary, "error")
        return False

    # --- память диалога (§5.10) ---

    async def memory(
        self, owner_id: UUID, dialog_id: UUID, kind: DialogKind, query: MemoryQuery
    ) -> MemoryEstimate:
        """Оценка памяти диалога для следующего вопроса без текста и вложений (§5.10.3).

        План строится так же, как для вопроса; история укладывается той же функцией и с
        тем же бюджетом, что в запросе к модели при включённом флаге.
        """
        if kind == "chat":
            plan = self._chat_plan("", query.params, [])
            search = query.search
        else:
            plan = await self._tools[kind].plan(owner_id, dialog_id, "", query.params)
            search = plan.selection is not None
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
            if dialog is None:
                raise not_found()
            messages = await uow.dialogs.history(owner_id, dialog_id, _ALL_POSITIONS)
            files = await uow.dialogs.message_attachments(
                owner_id, [message.id for message in messages if message.role == "user"]
            )
        messages = viewed(messages, self._history_views.get(kind))
        head = head_turn(dialog)
        return estimate_memory(
            messages,
            files,
            with_head_rules(plan.system, head, self._memory.head_rules_prompt),
            head,
            dialog.summary_until_position,
            plan.question,
            self._reserved_budget(plan, search),
            self._estimator,
            self._memory.keep_recent_pairs,
        )

    async def set_memory_mode(
        self, owner_id: UUID, dialog_id: UUID, message_id: UUID, mode: MemoryMode
    ) -> MessageView:
        """Сменить режим пары у её вопроса (§5.10.4).

        Пока в диалоге формируется ответ, режим не меняется: та же проверка под блокировкой
        строки диалога, что при отправке, с тем же ожиданием, затем `409`.
        """

        async def attempt() -> MessageView | None:
            async with self._uow_factory() as uow:
                dialog = await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True)
                question = (
                    None
                    if dialog is None
                    else await uow.dialogs.get_message(owner_id, dialog_id, message_id)
                )
                if dialog is None or question is None or question.role != "user":
                    raise not_found()
                idle, _ = await self._is_idle(uow, owner_id, dialog_id, dialog.kind)
                if not idle:
                    return None
                found = await uow.dialogs.message_attachments(owner_id, [question.id])
                files = found.get(question.id, [])
                if mode == "text_only" and not files:
                    raise validation_error([field_error("memory_mode", "invalid_format")])
                await uow.dialogs.set_memory_mode(owner_id, question.id, mode)
                question.memory_mode = mode
                return MessageView(question, files)

        return await self._when_idle(attempt)

    async def _locked_idle(self, uow: DialogUnitOfWork, owner_id: UUID, dialog_id: UUID) -> bool:
        """Под блокировкой строки диалога: свободен ли он; чужой или удалённый — `404`."""
        dialog = await uow.dialogs.get_dialog(owner_id, dialog_id, lock=True)
        if dialog is None:
            raise not_found()
        idle, _ = await self._is_idle(uow, owner_id, dialog_id, dialog.kind)
        return idle

    async def wait_idle(self, owner_id: UUID, dialog_id: UUID) -> None:
        """Дождаться, что в диалоге не формируется ответ, иначе `409` (§5.10.5, §5.10.8)."""

        async def attempt() -> bool | None:
            async with self._uow_factory() as uow:
                return True if await self._locked_idle(uow, owner_id, dialog_id) else None

        await self._when_idle(attempt)

    async def set_summary(
        self, owner_id: UUID, dialog_id: UUID, text: str, until_message_id: UUID
    ) -> SummaryView:
        """Применить или изменить пересказ: записать текст и границу вместо прежних (§5.10.6).

        Граница — ответ этого диалога, после которого не меньше `memory.keep_recent_pairs`
        вопросов. Пока в диалоге формируется ответ, пересказ не меняется — `409`.
        """
        text = text.strip()
        if not text:
            raise validation_error([field_error("text", "required")])
        if len(text) > self._memory.max_chars:
            raise validation_error([field_error("text", "too_long")])

        async def attempt() -> SummaryView | None:
            async with self._uow_factory() as uow:
                if not await self._locked_idle(uow, owner_id, dialog_id):
                    return None
                messages = await uow.dialogs.history(owner_id, dialog_id, _ALL_POSITIONS)
                until = next((item for item in messages if item.id == until_message_id), None)
                later = (
                    0
                    if until is None
                    else sum(
                        item.role == "user" and item.position > until.position for item in messages
                    )
                )
                if (
                    until is None
                    or until.role != "assistant"
                    or later < self._memory.keep_recent_pairs
                ):
                    raise validation_error([field_error("until_message_id", "unknown_value")])
                await uow.dialogs.set_summary(owner_id, dialog_id, text, until.position)
                covered = sum(item.position <= until.position for item in messages)
                return SummaryView(text, until.id, covered)

        return await self._when_idle(attempt)

    async def clear_summary(self, owner_id: UUID, dialog_id: UUID) -> None:
        """Отменить сжатие: пересказ и граница — `NULL`, сообщения не трогаются (§5.10.6)."""

        async def attempt() -> bool | None:
            async with self._uow_factory() as uow:
                if not await self._locked_idle(uow, owner_id, dialog_id):
                    return None
                await uow.dialogs.set_summary(owner_id, dialog_id, None, None)
                return True

        await self._when_idle(attempt)

    # --- формирование ответа: после статуса 200 ошибка возможна только событием ---

    async def _run(self, job: _Job) -> None:
        """Сформировать ответ, сохранить его и завершить поток — что бы ни случилось."""
        channel = job.channel
        channel.emit(
            "start",
            {"user_message_id": str(job.question.id), "assistant_message_id": str(job.answer_id)},
        )
        if job.announce_dangers:
            channel.emit("sql_question_check", {"dangers": list(job.announce_dangers)})
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
        if job.tool is not None and outcome.status in ("complete", "length_limit"):
            # Проверка блоков sql — только у ответа, полученного целиком (§7.1). Её сбой
            # ответу не мешает: он сохраняется без результата проверки.
            try:
                outcome.sql_check = await job.tool.review(job.plan, outcome.content)
            except Exception as error:
                self._log_failure("answer review failed", error)
        if outcome.error_code != "dialog_deleted" and not await self._save(job):
            # Ответ остался «формируется»; следующая отправка в диалог это исправит.
            outcome.status, outcome.error_code = "error", "internal_error"

        finished = outcome.status in ("complete", "length_limit")
        interrupted: Interruption | None = None
        if finished and outcome.sql_check is not None:
            # Текст ответа получен целиком и сохранён; дальше — только проверка (§5.5, шаг 6).
            channel.emit("sql_check", outcome.sql_check)
            if job.tool is not None and job.plan.sql_test_schema is not None:
                interrupted = await self._test_run(job, job.tool)
        editor = self._editors.get(job.kind)
        if finished and editor is not None and outcome.error_code != "dialog_deleted":
            interrupted = await self._edit(job, editor)
        if title_task is not None:
            if finished and interrupted is None:
                await asyncio.wait({title_task}, timeout=self._dialogs.title_wait_seconds)
            title_task.cancel()
            await asyncio.gather(title_task, return_exceptions=True)
        if interrupted is not None:
            # Проверка оборвана; ответ уже сохранён со своим состоянием и остаётся им. Поток,
            # закрытый клиентом, завершающего события не получает (§6.2).
            if interrupted != "stopped":
                channel.emit("error", {"code": interrupted, "message": ERROR_MESSAGES[interrupted]})
        elif finished:
            channel.emit("done", {"status": outcome.status})
        elif outcome.status == "error" and outcome.error_code is not None:
            code = outcome.error_code
            channel.emit("error", {"code": code, "message": ERROR_MESSAGES[code]})

    async def _test_run(self, job: _Job, tool: DialogTool) -> Interruption | None:
        """Проверить запросы сохранённого ответа на тестовых данных (§5.5, шаг 6; §7.4).

        Срок ответа на проверку не действует; наблюдение за соединением, сессией и
        диалогом продолжается. Возвращает причину обрыва. Итог пишется в `sql_run` ответа
        один раз; сбой самой проверки ответу не мешает — `sql_run` остаётся `null`.
        """
        latest: list[Mapping[str, Any]] = []

        def report(state: RunReport) -> None:
            # В событии — строки и сообщение СУБД, в хранимой записи с подключением — нет.
            latest[:] = [state.stored]
            job.channel.emit("sql_run", state.event)

        watch = StreamWatch(
            job.channel,
            self._sessions,
            job.session_id,
            lambda: self._answer_exists(job),
            math.inf,
            self._recheck_seconds,
        )
        try:
            _, reason = await watch.result(
                tool.test_run(job.owner_id, job.plan, job.outcome.content, report)
            )
        except Exception as error:
            self._log_failure("sql test run failed", error)
            return None
        finally:
            watch.close()
        if reason != "dialog_deleted" and latest:
            await self._persist(
                "sql run not saved",
                lambda uow: uow.dialogs.set_sql_run(job.owner_id, job.answer_id, latest[0]),
            )
        return reason

    # --- «Проверить снова» (1.0) ---

    async def recheck(
        self, user: CurrentUser, session_id: UUID, dialog_id: UUID, message_id: UUID, index: int
    ) -> EventChannel:
        """Проверить заново один блок ответа `sql` (§7.4, §6.6); поток — `sql_run`, `done`.

        Ответ — этого диалога, с `sql_run`, где есть блок `index` (иначе `404`). Диалог на
        время проверки занят (А-143): отметка ставится под блокировкой его строки.
        """
        tool = self._tools.get("sql")
        if tool is None or self._rechecker is None:
            raise not_found()
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(user.id, dialog_id)
            answer = await uow.dialogs.get_message(user.id, dialog_id, message_id)
            if dialog is None or dialog.kind != "sql" or answer is None:
                raise not_found()
            blocks = (answer.sql_run or {}).get("blocks") or []
            if answer.role != "assistant" or all(item.get("index") != index for item in blocks):
                raise not_found()
            earlier = await uow.dialogs.history(user.id, dialog_id, answer.position)
        if not earlier or earlier[-1].role != "user":
            raise not_found()
        plan = await self._rechecker.recheck_plan(user.id, earlier[-1].params)

        async def attempt() -> bool | None:
            marked = False
            try:
                async with self._uow_factory() as uow:
                    if await uow.dialogs.get_dialog(user.id, dialog_id, lock=True) is None:
                        raise not_found()
                    idle, _ = await self._is_idle(uow, user.id, dialog_id, "sql")
                    if not idle:
                        return None
                    # До фиксации, под блокировкой: как отметка ответа (§5.5).
                    self._rechecking.add(dialog_id)
                    marked = True
            except BaseException:
                if marked:
                    self._rechecking.discard(dialog_id)
                raise
            return True

        await self._when_idle(attempt)
        channel = EventChannel()
        task = asyncio.create_task(
            self._recheck(
                tool,
                plan,
                user.id,
                session_id,
                dialog_id,
                message_id,
                answer.content,
                index,
                channel,
            )
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return channel

    async def _recheck(
        self,
        tool: DialogTool,
        plan: AnswerPlan,
        owner_id: UUID,
        session_id: UUID,
        dialog_id: UUID,
        answer_id: UUID,
        content: str,
        index: int,
        channel: EventChannel,
    ) -> None:
        """Ход перепроверки и сохранение итога блока; отметка снимается при любом исходе."""
        latest: list[Mapping[str, Any]] = []

        def report(state: RunReport) -> None:
            latest[:] = [state.stored]
            channel.emit("sql_run", state.event)

        async def exists() -> bool:
            async with self._uow_factory() as uow:
                return await uow.dialogs.message_exists(owner_id, answer_id)

        try:
            watch = StreamWatch(
                channel, self._sessions, session_id, exists, math.inf, self._recheck_seconds
            )
            try:
                _, reason = await watch.result(
                    tool.test_run(owner_id, plan, content, report, only=index)
                )
            except Exception as error:
                self._log_failure("sql recheck failed", error)
                channel.emit(
                    "error", {"code": "internal_error", "message": ERROR_MESSAGES["internal_error"]}
                )
                return
            finally:
                watch.close()
            blocks = latest[0]["blocks"] if latest else []
            final = blocks[0] if blocks else None
            if reason != "dialog_deleted" and final is not None and final["status"] != "stopped":
                await self._persist(
                    "sql recheck not saved",
                    lambda uow: self._replace_block(uow, owner_id, dialog_id, answer_id, final),
                )
            if reason is None:
                channel.emit("done", {})
            elif reason != "stopped":
                channel.emit("error", {"code": reason, "message": ERROR_MESSAGES[reason]})
        finally:
            self._rechecking.discard(dialog_id)
            channel.finish()

    @staticmethod
    async def _replace_block(
        uow: DialogUnitOfWork,
        owner_id: UUID,
        dialog_id: UUID,
        answer_id: UUID,
        block: Mapping[str, Any],
    ) -> None:
        """Заменить запись блока в сохранённом `sql_run` ответа (А-139)."""
        answer = await uow.dialogs.get_message(owner_id, dialog_id, answer_id)
        if answer is None or answer.sql_run is None:
            return
        blocks = [
            dict(block) if item.get("index") == block["index"] else item
            for item in answer.sql_run.get("blocks") or []
        ]
        await uow.dialogs.set_sql_run(owner_id, answer_id, {**answer.sql_run, "blocks": blocks})

    async def _edit(self, job: _Job, editor: AnswerEditor) -> Interruption | None:
        """Правка по сохранённому ответу и цикл исправления (§5.5, шаг 6; §7.5.10–7.5.11).

        Как проверка SQL: срок ответа не действует, наблюдение за соединением, сессией и
        диалогом продолжается; обрыв — правки нет, ответ остаётся как сохранён. Сбой самой
        правки ответу не мешает. Событие `code_edit` — перед `done`.
        """
        status: Literal["complete", "length_limit"] = (
            "length_limit" if job.outcome.status == "length_limit" else "complete"
        )
        watch = StreamWatch(
            job.channel,
            self._sessions,
            job.session_id,
            lambda: self._answer_exists(job),
            math.inf,
            self._recheck_seconds,
        )
        try:
            edit, reason = await watch.result(
                editor.propose(
                    job.owner_id,
                    job.dialog_id,
                    job.answer_id,
                    job.question.params,
                    job.outcome.content,
                    status,
                    lambda state: job.channel.emit("code_fix", state),
                )
            )
        except Exception as error:
            self._log_failure("code edit failed", error)
            return None
        finally:
            watch.close()
        if reason is None and edit is not None:
            job.channel.emit("code_edit", edit)
        return reason

    async def _drop_leftovers(self, job: _Job) -> None:
        """Удалить файлы неотправленных остатков; их строки удалены вместе с вопросом.

        Сбой здесь ответу не мешает: файл без строки никому не виден.
        """
        for item in job.leftovers:
            try:
                await self._files.delete(item)
            except OSError as error:
                self._log_failure("leftover file not deleted", error)

    @staticmethod
    def _log_failure(message: str, error: Exception) -> None:
        """Тип исключения и места в коде; текст исключения в журнал не идёт."""
        logger.error(
            message, extra={"error_type": type(error).__name__, "trace": code_locations(error)}
        )

    async def _save(self, job: _Job) -> bool:
        """Сохранить ответ; `False` — не удалось и после повторов."""
        outcome = job.outcome
        cited = None
        if outcome.found is not None:
            # Хранятся только источники, на которые в тексте есть сноска вне кода (§5.5).
            numbers = cited_numbers(outcome.content, [source.n for source in outcome.found])
            cited = [_source_data(source) for source in outcome.found if source.n in numbers]
        return await self._persist(
            "answer not saved",
            lambda uow: uow.dialogs.finish_answer(
                job.owner_id,
                job.answer_id,
                content=outcome.content,
                status=outcome.status,
                error_code=outcome.error_code if outcome.status == "error" else None,
                reasoning=outcome.reasoning or None,
                reasoning_seconds=outcome.reasoning_seconds,
                sources=cited,
                sources_found=None if outcome.found is None else len(outcome.found),
                sql_check=outcome.sql_check,
                dropped_messages=outcome.dropped_messages,
            ),
        )

    async def _persist(
        self, failure: str, write: Callable[[DialogUnitOfWork], Awaitable[None]]
    ) -> bool:
        """Записать итог в своей транзакции; при сбое базы — несколько попыток (§5.5)."""
        for attempt in range(_SAVE_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_SAVE_RETRY_SECONDS * attempt)
            try:
                async with self._uow_factory() as uow:
                    await write(uow)
                return True
            except Exception as error:
                self._log_failure(failure, error)
        return False

    async def _produce(self, job: _Job) -> None:
        """Собрать запрос и получить ответ; итог записывается в `job.outcome`."""
        watch = StreamWatch(
            job.channel,
            self._sessions,
            job.session_id,
            lambda: self._answer_exists(job),
            self._dialogs.generation_timeout_seconds,
            self._recheck_seconds,
        )
        try:
            await self._answer(job, watch)
        finally:
            watch.close()

    def _interrupted(self, job: _Job, reason: Interruption) -> None:
        """Записать итог прерванного ответа: остановка либо сбой с кодом причины."""
        if reason == "stopped":
            job.outcome.status, job.outcome.error_code = "stopped", None
        else:
            job.outcome.status, job.outcome.error_code = "error", reason

    async def _answer(self, job: _Job, watch: StreamWatch) -> None:
        outcome, plan = job.outcome, job.plan
        system = plan.system
        question = plan.question
        async with self._uow_factory() as uow:
            dialog = await uow.dialogs.get_dialog(job.owner_id, job.dialog_id)
            earlier = await uow.dialogs.history(job.owner_id, job.dialog_id, job.question.position)
            files = await uow.dialogs.message_attachments(
                job.owner_id, [message.id for message in earlier if message.role == "user"]
            )
        if dialog is None:
            self._interrupted(job, "dialog_deleted")
            return
        earlier = viewed(earlier, self._history_views.get(job.kind))
        # Сохранённые режимы пар, пересказ и перенесённое учитываются при любом значении
        # флага: это содержимое диалога (§5.10.9). Пересказанные сообщения в запрос не идут.
        summarized_until = dialog.summary_until_position
        head = head_turn(dialog)
        # Правило о головном сообщении — сразу после системного, правила базы знаний — за
        # ним (§5.10.2). Сами пересказ и перенесённое в системное сообщение не попадают.
        system = with_head_rules(system, head, self._memory.head_rules_prompt)
        planned_system = system
        if plan.selection is not None:
            retrieval = await self._search(
                job, plan.selection, search_history(earlier, summarized_until), watch
            )
            if retrieval is None:
                return
            # Правила — в конец системного сообщения; найденное — первым блоком вопроса,
            # перед вложениями и текстом пользователя. В историю оно не попадает (§5.4).
            system = f"{system}\n\n{retrieval.rules}"
            if retrieval.context:
                question = Turn("user", f"{retrieval.context}\n\n{question.text}", question.images)

        history = history_turns(earlier, files, summarized_until)
        # С памятью диалога история укладывается с резервом под найденное, а не по его
        # фактическому объёму, — как в оценке памяти; без неё — как в 0.6 (§5.10.2).
        if self._memory.enabled:
            fit_system, fit_question = planned_system, plan.question
            budget = self._reserved_budget(plan, plan.selection is not None)
        else:
            fit_system, fit_question, budget = system, question, self._budget(plan)
        dropped = fit_memory(
            fit_system, history, fit_question, budget, self._estimator, head=head
        ).dropped
        if dropped:
            job.channel.emit("context_truncated", {"dropped_messages": dropped})
        # Головное сообщение не отбрасывается никогда, в том числе при повторе.
        lead = [] if head is None else [head]

        for attempt in range(2):
            outcome.dropped_messages = dropped
            request = ChatRequest(
                await self._model_messages(system, [*lead, *history[dropped:], question]),
                plan.effort,
                plan.max_tokens,
                include_usage=self._memory.enabled,
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
                dropped = fit_memory(
                    fit_system, history, fit_question, budget, self._estimator, more, head=head
                ).dropped
        outcome.status, outcome.error_code = "error", "message_too_long"

    async def _search(
        self,
        job: _Job,
        selection: KnowledgeSelection,
        history: Sequence[DialogTurn],
        watch: StreamWatch,
    ) -> Retrieval | None:
        """Найти места в базе знаний; `None` — ответа не будет, итог записан.

        В порт уходят текст вопроса как есть, пользователь из сессии, выбор из параметров
        вопроса и история диалога — по ней порт сам переписывает уточняющий вопрос (§5.4).
        Остановка, конец сессии и срок прерывают и поиск.
        """
        outcome = job.outcome
        job.channel.emit("search_started", {})
        try:
            retrieval, reason = await watch.result(
                self._knowledge.retrieve(job.question.content, job.owner_id, selection, history)
            )
        except KnowledgeUnavailableError:
            # Ответ без базы знаний сервер не подменяет.
            outcome.status, outcome.error_code = "error", "knowledge_unavailable"
            return None
        if retrieval is None:
            self._interrupted(job, reason or "stopped")
            return None
        outcome.found = tuple(retrieval.sources)
        job.channel.emit(
            "sources", {"sources": [_source_data(source) for source in retrieval.sources]}
        )
        return retrieval

    async def _model_messages(self, system: str, turns: Sequence[Turn]) -> list[ModelMessage]:
        messages = [ModelMessage("system", [TextPart(system)])]
        loop = asyncio.get_running_loop()
        for turn in turns:
            parts: list[TextPart | ImagePart] = [TextPart(turn.text)] if turn.text else []
            for image in turn.images:
                parts.append(
                    await loop.run_in_executor(self._document_executor, self._load_image, image)
                )
            messages.append(ModelMessage(turn.role, parts))
        return messages

    def _load_image(self, image: ImageRef) -> ImagePart:
        """Изображение-вложение уходит как есть, страница-скан PDF — отрисованной в PNG."""
        path = self._storage.path(image.storage_key)
        if image.media_type == "image/jpeg" or image.media_type == "image/png":
            return ImagePart(path.read_bytes(), image.media_type)
        return ImagePart(self._reader.page_image(path, image.media_type, image.page), "image/png")

    async def _consume(self, job: _Job, request: ChatRequest, watch: StreamWatch) -> None:
        """Читать поток модели, пока не случится одно из: конец, остановка, сбой, срок."""
        channel, outcome = job.channel, job.outcome
        try:
            async with aclosing(watched_stream(self._model, request, watch)) as events:
                async for event in events:
                    if isinstance(event, str):
                        self._interrupted(job, event)
                    elif isinstance(event, Finished):
                        outcome.status = "length_limit" if event.reason == "length" else "complete"
                        outcome.error_code = None
                    elif isinstance(event, ReasoningDelta):
                        outcome.reasoning += event.text
                        channel.emit("reasoning_delta", {"text": event.text})
                    elif isinstance(event, Usage):
                        self._log_usage(job.answer_id, request, event.prompt_tokens)
                    elif isinstance(event, ContentDelta):
                        if outcome.reasoning and outcome.reasoning_seconds is None:
                            outcome.reasoning_seconds = round(time.monotonic() - watch.started)
                        outcome.content += event.text
                        channel.emit("delta", {"text": event.text})
        except ModelOverloadedError:
            outcome.status, outcome.error_code = "error", "model_overloaded"
        except ModelUnavailableError:
            outcome.status, outcome.error_code = "error", "model_unavailable"

    def _log_usage(self, answer_id: UUID, request: ChatRequest, prompt_tokens: int) -> None:
        """Строка калибровки «оценка — факт» (§5.10.10): только числа и идентификатор."""
        texts = [
            part.text
            for message in request.messages
            for part in message.parts
            if isinstance(part, TextPart)
        ]
        images = sum(
            isinstance(part, ImagePart) for message in request.messages for part in message.parts
        )
        estimated = sum(self._estimator.text(text) for text in texts)
        logger.info(
            "prompt usage",
            extra={
                "answer_id": str(answer_id),
                "estimated_tokens": estimated + self._estimator.image() * images,
                "prompt_tokens": prompt_tokens,
                "text_chars": sum(len(text) for text in texts),
                "images": images,
            },
        )

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


def _source_data(source: Source) -> dict[str, Any]:
    """Источник в виде объекта `Source` контракта (§5.1)."""
    return {
        "n": source.n,
        "document_id": str(source.document_id),
        "document_title": source.document_title,
        "scope": source.scope,
        "page": source.page,
        "timecode_seconds": source.timecode_seconds,
        "image_id": None if source.image_id is None else str(source.image_id),
        "fragment_id": str(source.fragment_id),
        "quote": source.quote,
    }
