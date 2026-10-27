"""Тела запросов и ответов диалогов (docs/portal-api.md §5.1–5.3)."""

from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    model_serializer,
)

from portal.core.schemas import ApiTime, RequestModel
from portal.databases.schemas import DbSelectionOut, SelectionRequest
from portal.databases.service import SelectionInput
from portal.dialogs.domain import (
    AnswerMode,
    Attachment,
    Dialog,
    DialogKind,
    Docparse,
    MemoryMode,
    MessageRole,
    MessageStatus,
    SummaryStatus,
    TranscriptionStatus,
)
from portal.dialogs.memory import MemoryEstimate, MemoryLevel
from portal.dialogs.service import CarriedView, DialogDetails, MessageView, SummaryView
from portal.files.ports import MediaType, RecordingType
from portal.kb.ports import Collection


class CarriedRequest(RequestModel):
    """Перенесённое при создании диалога (§5.10.7); длину текста проверяет сценарий."""

    text: str
    source_dialog_id: UUID


class CreateDialogRequest(RequestModel):
    """`POST /api/dialogs`; диалог `docparse` создаётся только запуском разбора."""

    kind: Literal["chat", "sql", "cogis"]
    carried: CarriedRequest | None = None


class DraftRequest(RequestModel):
    """`POST /api/dialogs/{id}/memory/draft` (§5.10.5); `until_message_id` — только у переноса."""

    purpose: Literal["compress", "transfer"]
    until_message_id: UUID | None = None


class SummaryRequest(RequestModel):
    """`PUT /api/dialogs/{id}/memory/summary` (§5.10.6); длину текста проверяет сценарий."""

    text: str
    until_message_id: UUID


class RenameDialogRequest(RequestModel):
    """`PATCH /api/dialogs/{id}`: `{ title?, db? }`, хотя бы одно; `db` — только у `sql`."""

    title: str | None = None
    db: SelectionRequest | None = None

    def selection(self) -> SelectionInput | None:
        """`db` для сценария; `null` — снять выбор."""
        if self.db is None:
            return None
        return SelectionInput(self.db.connection_id, self.db.pairs())


class ChatMessageRequest(RequestModel):
    """Тело сообщения в диалоге `chat` (§5.3)."""

    content: str
    attachment_ids: tuple[UUID, ...] = ()
    mode: AnswerMode
    collections: tuple[Collection, ...]
    tags: tuple[str, ...] = ()


class SqlMessageRequest(RequestModel):
    """Тело сообщения в диалоге `sql`: все поля обязательны, `schema_id: null` — без схемы."""

    content: str
    action: Literal["write", "explain", "debug", "optimize"]
    dialect: str
    schema_id: UUID | None
    test_run: StrictBool


class CogisMessageRequest(RequestModel):
    """Тело сообщения в диалоге `cogis`."""

    content: str
    action: Literal["write", "explain", "debug"]


class CodeTargetRequest(RequestModel):
    """Выделенный фрагмент файла проекта; границы проверяет сценарий (§7.5.8)."""

    file_id: UUID
    start_line: int
    end_line: int


class CodeMessageRequest(RequestModel):
    """Тело сообщения в диалоге `code` (§7.5.8): `file_ids` и `target` обязательны, `null` — нет.

    С `preview_fix` (§7.5.16) поля `content` нет: текст вопроса собирает сервер; само поле
    проверяет сценарий.
    """

    content: str | None = None
    action: Literal["write", "explain", "fix"]
    file_ids: list[UUID] | None
    target: CodeTargetRequest | None
    preview_fix: dict[str, Any] | None = None


class DocparseMessageRequest(RequestModel):
    """Вопрос по разобранному документу."""

    content: str


class MemoryModeRequest(RequestModel):
    """`PATCH /api/dialogs/{id}/messages/{message_id}`: режим пары (§5.10.4)."""

    memory_mode: MemoryMode


class ChatMemoryQuery(BaseModel):
    """Строка запроса оценки памяти в чате; параметры другого вида не читаются."""

    mode: AnswerMode
    search: Literal["true", "false"]


class SqlMemoryQuery(BaseModel):
    """Строка запроса оценки памяти в SQL-помощнике; нет `schema_id` — без схемы."""

    action: Literal["write", "explain", "debug", "optimize"]
    dialect: str
    schema_id: UUID | None = None


class CogisMemoryQuery(BaseModel):
    """Строка запроса оценки памяти в помощнике CoGIS."""

    action: Literal["write", "explain", "debug"]


class CodeMemoryQuery(BaseModel):
    """Строка запроса оценки памяти помощника проекта; нет `files` — все файлы (§7.5.8)."""

    action: Literal["write", "explain", "fix"]
    files: str | None = None


class SqlRegenerateRequest(RequestModel):
    """Необязательное тело повторной генерации в диалоге `sql`: замена схемы (§5.6)."""

    schema_id: UUID | None


class SqlRecheckRequest(RequestModel):
    """Тело «Проверить снова»: номер блока в `sql_run` ответа (§7.4)."""

    index: int


class DocparseFieldOut(BaseModel):
    """Реквизит разбора; `value: null` — в документе его нет."""

    title: str
    value: str | None


class DocparseOut(BaseModel):
    """Результат разбора — поле `docparse` диалога (§7.3)."""

    file_name: str
    page_count: int | None
    template_id: str
    template_title: str
    free_form: bool
    fields: list[DocparseFieldOut]
    summary: str
    summary_status: SummaryStatus

    @classmethod
    def of(cls, docparse: Docparse) -> "DocparseOut":
        """Собрать из разбора; пока содержание пишется, поле `summary` пусто."""
        return cls(
            file_name=docparse.file_name,
            page_count=docparse.page_count,
            template_id=docparse.template_id,
            template_title=docparse.template_title,
            free_form=docparse.free_form,
            fields=[DocparseFieldOut.model_validate(item) for item in docparse.fields],
            summary=docparse.summary,
            summary_status=docparse.summary_status,
        )


class DialogOut(BaseModel):
    """Объект `Dialog`."""

    id: UUID
    kind: DialogKind
    title: str | None
    created_at: ApiTime
    updated_at: ApiTime

    @classmethod
    def of(cls, dialog: Dialog) -> "DialogOut":
        """Собрать из диалога."""
        return cls(
            id=dialog.id,
            kind=dialog.kind,
            title=dialog.title,
            created_at=dialog.created_at,
            updated_at=dialog.updated_at,
        )


class SummaryOut(BaseModel):
    """Применённый пересказ — поле `summary` диалога (§5.1)."""

    text: str
    until_message_id: UUID
    messages: int

    @classmethod
    def of(cls, summary: SummaryView) -> "SummaryOut":
        """Собрать из пересказа."""
        return cls(
            text=summary.text,
            until_message_id=summary.until_message_id,
            messages=summary.messages,
        )


class CarriedOut(BaseModel):
    """Перенесённое — поле `carried` диалога (§5.1).

    `source_project_id` есть только у источника вида `code`: у других видов поля нет.
    """

    text: str
    source_dialog_id: UUID | None
    source_kind: DialogKind | None
    source_title: str | None
    source_project_id: UUID | None = None

    @model_serializer(mode="wrap")
    def _drop_project(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if self.source_kind != "code":
            data.pop("source_project_id", None)
        return data


class DialogMemoryOut(DialogOut):
    """`GET /api/dialogs/{id}`: диалог с пересказом и перенесённым (§5.1)."""

    summary: SummaryOut | None
    carried: CarriedOut | None

    @classmethod
    def of_details(cls, details: DialogDetails) -> "DialogMemoryOut":
        """Собрать из подробностей диалога; у разбора — вместе с его результатом."""
        base = DialogOut.of(details.dialog).model_dump()
        summary = None if details.summary is None else SummaryOut.of(details.summary)
        carried = None if details.carried is None else _carried(details.carried)
        if details.docparse is not None:
            return DialogDetailsOut(
                **base,
                summary=summary,
                carried=carried,
                docparse=DocparseOut.of(details.docparse),
            )
        return cls(**base, summary=summary, carried=carried)


def _carried(carried: CarriedView) -> CarriedOut:
    return CarriedOut(
        text=carried.text,
        source_dialog_id=carried.source_dialog_id,
        source_kind=carried.source_kind,
        source_title=carried.source_title,
        source_project_id=carried.source_project_id,
    )


class SqlDialogOut(DialogOut):
    """`Dialog` вида `sql` в ответе `PATCH` (1.0): с выбором подключения."""

    db: DbSelectionOut | None


class SqlDialogMemoryOut(DialogMemoryOut):
    """`GET /api/dialogs/{id}` вида `sql` (1.0): с выбором подключения."""

    db: DbSelectionOut | None


class DialogDetailsOut(DialogMemoryOut):
    """`GET /api/dialogs/{id}` для вида `docparse`: ещё и результат разбора."""

    docparse: DocparseOut


class DialogListOut(BaseModel):
    """Часть списка диалогов."""

    items: list[DialogOut]
    next_cursor: str | None


class TranscriptionOut(BaseModel):
    """Состояние расшифровки вложения-записи (§5.1)."""

    status: TranscriptionStatus
    error_code: str | None


class AttachmentOut(BaseModel):
    """Объект `Attachment`; `transcription: null` — вложение не запись."""

    id: UUID
    file_name: str
    media_type: MediaType | RecordingType
    page_count: int | None
    image_count: int
    duration_seconds: int | None
    transcription: TranscriptionOut | None
    created_at: ApiTime

    @classmethod
    def of(cls, attachment: Attachment) -> "AttachmentOut":
        """Собрать из вложения."""
        return cls(
            id=attachment.id,
            file_name=attachment.file_name,
            media_type=attachment.media_type,
            page_count=attachment.page_count,
            image_count=attachment.image_count,
            duration_seconds=attachment.duration_seconds,
            transcription=None
            if attachment.transcription_status is None
            else TranscriptionOut(
                status=attachment.transcription_status, error_code=attachment.transcription_error
            ),
            created_at=attachment.created_at,
        )


class SourceOut(BaseModel):
    """Объект `Source`: источник, на который в ответе есть сноска `[n]`.

    В снимках, сохранённых до версии 0.15, полей `timecode_seconds` и `image_id` нет —
    они отдаются как `null` (§5.1).
    """

    n: int
    document_id: UUID
    document_title: str
    scope: Collection
    page: int | None
    timecode_seconds: int | None = None
    image_id: UUID | None = None
    fragment_id: UUID
    quote: str


def _sql_run(stored: dict[str, Any] | None) -> dict[str, Any] | None:
    """Блоки `sql_run`, сохранённые до 1.0, — с полями 1.0 по умолчанию (§9.32)."""
    if stored is None:
        return None
    blocks = []
    for block in stored.get("blocks") or []:
        result, fixture = block.get("result"), block.get("fixture")
        if result is not None and "row_count" not in result:
            rows = result.get("rows")
            result = {**result, "row_count": None if rows is None else len(rows)}
        blocks.append(
            {
                **block,
                "db": block.get("db"),
                "fixture": None
                if fixture is None
                else [
                    {"origin": "invented", "invented_reason": None, **table} for table in fixture
                ],
                "result": result,
                "error_kind": block.get("error_kind", "message" if block.get("error") else None),
            }
        )
    return {**stored, "blocks": blocks}


class MessageOut(BaseModel):
    """Объект `Message`."""

    id: UUID
    role: MessageRole
    content: str
    status: MessageStatus
    error_code: str | None
    reasoning: str | None
    reasoning_seconds: int | None
    attachments: list[AttachmentOut]
    sources: list[SourceOut] | None
    sources_found: int | None
    sql_check: dict[str, Any] | None
    sql_run: dict[str, Any] | None
    sql_dangers: list[str] | None
    dropped_messages: int
    memory_mode: MemoryMode
    code_target: dict[str, Any] | None
    code_edit: dict[str, Any] | None
    created_at: ApiTime

    @classmethod
    def of(cls, view: MessageView) -> "MessageOut":
        """Собрать из сообщения с вложениями."""
        message = view.message
        return cls(
            id=message.id,
            role=message.role,
            content=message.content,
            status=message.status,
            error_code=message.error_code,
            reasoning=message.reasoning,
            reasoning_seconds=message.reasoning_seconds,
            attachments=[AttachmentOut.of(item) for item in view.attachments],
            sources=None
            if message.sources is None
            else [SourceOut.model_validate(source) for source in message.sources],
            sources_found=message.sources_found,
            sql_check=message.sql_check,
            sql_run=_sql_run(message.sql_run),
            sql_dangers=message.sql_dangers,
            dropped_messages=message.dropped_messages,
            memory_mode=message.memory_mode,
            code_target=_code_target(message.params) if message.role == "user" else None,
            code_edit=message.code_edit,
            created_at=message.created_at,
        )


def _code_target(params: dict[str, Any]) -> dict[str, Any] | None:
    """Фрагмент вопроса диалога `code` без снимка текста (§5.1, §7.5.8)."""
    target = params.get("target")
    if not isinstance(target, dict):
        return None
    return {key: target[key] for key in ("file_id", "path", "start_line", "end_line")}


class MessageListOut(BaseModel):
    """Часть списка сообщений."""

    items: list[MessageOut]
    next_cursor: str | None


class MemoryPairOut(BaseModel):
    """Пара «вопрос + ответ» в объекте `Memory` (§5.10.3)."""

    question_id: UUID
    answer_id: UUID
    question_preview: str
    answer_preview: str
    attachments: int
    memory_mode: MemoryMode
    tokens: int
    dropped: bool
    summarized: bool


class PairRangeOut(BaseModel):
    """`Memory.compress_pairs`: номера пар следующего сжатия, включительно."""

    from_: int = Field(serialization_alias="from")
    to: int


class MemoryOut(BaseModel):
    """Объект `Memory`: заполненность памяти диалога и его пары (§5.10.3)."""

    level: MemoryLevel
    percent: int
    used_tokens: int
    limit_tokens: int
    compress_pairs: PairRangeOut | None
    pairs: list[MemoryPairOut]

    @classmethod
    def of(cls, estimate: MemoryEstimate) -> "MemoryOut":
        """Собрать из оценки."""
        return cls(
            level=estimate.level,
            percent=estimate.percent,
            used_tokens=estimate.used_tokens,
            limit_tokens=estimate.limit_tokens,
            compress_pairs=None
            if estimate.compress_pairs is None
            else PairRangeOut(from_=estimate.compress_pairs.first, to=estimate.compress_pairs.last),
            pairs=[
                MemoryPairOut.model_validate(pair, from_attributes=True) for pair in estimate.pairs
            ],
        )
