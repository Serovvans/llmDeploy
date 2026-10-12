"""Единственное место чтения конфигурации: YAML, файл-накладка и секреты из окружения.

Порядок (docs/portal-api.md §13.5): все `*.yaml` каталога конфигурации объединяются,
поверх накладывается необязательный файл-накладка; словари объединяются по ключам,
списки и простые значения заменяются целиком.
"""

import base64
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    SecretStr,
    field_validator,
    model_validator,
)

# Значение high модель не принимает (docs/design.md §3.1): настройки отвергают его при старте.
ReasoningEffort = Literal["low", "medium", "xhigh"]

CONFIG_DIR = Path("config")
OVERRIDE_PATH = Path("/etc/portal/config.override.yaml")

_SECRET_KEY_BYTES = 32
# Слова «Документ: », «Раздел: » и переводы строк в тексте для поиска (docs/portal-api.md §10.1).
_SEARCH_TEXT_LABELS_CHARS = 22


class ConfigError(Exception):
    """Конфигурация или окружение не позволяют запустить сервис."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerSettings(_Section):
    """Пределы приёма запросов (docs/portal-api.md §1.3)."""

    json_body_max_bytes: int = Field(ge=1)
    multipart_overhead_bytes: int = Field(ge=0)


class DatabaseSettings(_Section):
    """Адрес PostgreSQL; пароль приходит из окружения."""

    host: str
    port: int
    name: str
    user: str


class Argon2Settings(_Section):
    """Параметры Argon2id."""

    time_cost: int = Field(ge=1)
    memory_cost_kib: int = Field(ge=8)
    parallelism: int = Field(ge=1)
    workers: int = Field(ge=1)


class PasswordSettings(_Section):
    """Политика паролей (docs/portal-design.md §4: не короче 12 символов)."""

    min_length: int = Field(ge=12)
    max_length: int
    common_passwords_file: str
    argon2: Argon2Settings

    @model_validator(mode="after")
    def _check_lengths(self) -> Self:
        if self.max_length < self.min_length:
            raise ValueError("auth.password.max_length меньше min_length")
        return self


class SessionSettings(_Section):
    """Сроки сессии (docs/portal-api.md §2.6)."""

    absolute_ttl_hours: int = Field(ge=1)
    idle_ttl_minutes: int = Field(ge=1)
    login_step_ttl_minutes: int = Field(ge=1)
    stream_recheck_seconds: int = Field(ge=1)


class LockoutSettings(_Section):
    """Блокировка логина после неудач подряд (docs/portal-api.md §2.5)."""

    max_failures: int = Field(ge=1)
    base_seconds: int = Field(ge=1)
    max_seconds: int = Field(ge=1)
    reset_after_minutes: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_reset_outlasts_lock(self) -> Self:
        if self.reset_after_minutes * 60 <= self.max_seconds:
            raise ValueError("auth.lockout.reset_after_minutes должен быть больше max_seconds")
        return self


class IpLimitSettings(_Section):
    """Ограничение неудачных попыток с одного адреса."""

    max_failures: int = Field(ge=1)
    window_seconds: int = Field(ge=1)


class TotpSettings(_Section):
    """Второй фактор."""

    window_steps: int = Field(ge=0)
    issuer: str = Field(min_length=1)
    backup_codes_count: int = Field(ge=1)


class AuthSettings(_Section):
    """Вход, сессии и ограничение перебора."""

    password: PasswordSettings
    session: SessionSettings
    lockout: LockoutSettings
    ip_limit: IpLimitSettings
    totp: TotpSettings


class LlmSettings(_Section):
    """Обращение к модели через Bifrost (docs/portal-api.md §12.1)."""

    base_url: str = Field(min_length=1)
    chat_model: str = Field(min_length=1)
    safety_margin_tokens: int = Field(ge=0)
    first_token_timeout_seconds: int = Field(ge=1)
    image_max_side_px: int = Field(ge=1)
    context_overflow_marker: str = Field(min_length=1)
    chars_per_token: float = Field(gt=0)
    tokens_per_image: int = Field(ge=1)
    embedding_model: str = Field(min_length=1)
    recognition_max_tokens: int = Field(ge=1)
    recognition_parallel_requests: int = Field(ge=1)
    recognition_attempts: int = Field(ge=1)
    recognition_retry_pause_seconds: float = Field(ge=0)
    recognition_system_prompt: str = Field(min_length=1)


class FilesSettings(_Section):
    """Хранение и чтение загруженных файлов (docs/portal-api.md §1.5)."""

    root: Path
    text_layer_min_chars: int = Field(ge=1)
    text_layer_min_valid_share: float = Field(ge=0, le=1)
    image_max_pixels: int = Field(ge=1)
    docx_max_unpacked_bytes: int = Field(ge=1)
    docx_max_xml_bytes: int = Field(ge=1)
    pdf_render_scale: float = Field(gt=0)
    reader_workers: int = Field(ge=1)


class DialogsSettings(_Section):
    """Диалоги и поток ответа."""

    message_max_chars: int = Field(ge=1)
    generation_timeout_seconds: int = Field(ge=1)
    stop_grace_seconds: int = Field(ge=0)
    title_wait_seconds: int = Field(ge=0)
    title_max_tokens: int = Field(ge=1)
    keepalive_seconds: float = Field(gt=0, le=15)
    empty_ttl_hours: int = Field(ge=1)
    title_system_prompt: str = Field(min_length=1)


class ChatOutputTokens(_Section):
    """Предел длины ответа по режимам; в него входят и размышления."""

    fast: int = Field(ge=1)
    thorough: int = Field(ge=1)


class ChatSettings(_Section):
    """Чат: вложения, пределы ответа, системное сообщение."""

    attachment_max_bytes: int = Field(ge=1)
    attachment_max_pages: int = Field(ge=1)
    attachment_extensions: tuple[str, ...]
    max_attachments: int = Field(ge=1)
    max_output_tokens: ChatOutputTokens
    system_prompt: str = Field(min_length=1)


class KbIndexingSettings(_Section):
    """Пределы конвейера индексации."""

    document_max_chars: int = Field(ge=1)


class KbChunkingSettings(_Section):
    """Разбиение текста страницы на фрагменты."""

    max_chars: int = Field(ge=1)
    overlap_chars: int = Field(ge=0)
    heading_patterns: tuple[str, ...]
    section_max_chars: int = Field(ge=1)

    @field_validator("heading_patterns")
    @classmethod
    def _check_heading_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(
                    "kb.chunking.heading_patterns: негодное регулярное выражение"
                ) from error
        return patterns

    @model_validator(mode="after")
    def _check_overlap(self) -> Self:
        if self.overlap_chars * 2 > self.max_chars:
            raise ValueError("kb.chunking.overlap_chars не должен превышать половины max_chars")
        return self


class KbEmbeddingsSettings(_Section):
    """Эмбеддинги (docs/portal-api.md §10.1, §12.1)."""

    dimension: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    max_input_chars: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    query_instruction: str


class KbSearchSettings(_Section):
    """Гибридный поиск (docs/portal-api.md §10.4)."""

    top_k: int = Field(ge=1)
    prefetch_limit: int = Field(ge=1)
    lexical_slots: int = Field(ge=0)
    neighbors: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_slots(self) -> Self:
        if self.lexical_slots > self.top_k:
            raise ValueError("kb.search.lexical_slots не должен превышать top_k")
        return self


class KbQdrantSettings(_Section):
    """Адрес и коллекция Qdrant; ключ приходит из окружения."""

    url: str = Field(min_length=1)
    collection: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    upsert_batch_size: int = Field(ge=1)


class KbWorkerSettings(_Section):
    """Очередь заданий (docs/portal-api.md §11)."""

    concurrency: int = Field(ge=1)
    max_attempts: int = Field(ge=1)
    retry_base_seconds: int = Field(ge=1)
    retry_max_seconds: int = Field(ge=1)
    lease_seconds: int = Field(ge=3)
    poll_seconds: float = Field(gt=0)
    call_attempts: int = Field(ge=1)
    call_retry_pause_seconds: float = Field(ge=0)
    heartbeat_file: Path
    heartbeat_stale_seconds: float = Field(gt=0)
    chat_slots: int = Field(ge=1)
    background_llm_parallel: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_heartbeat(self) -> Self:
        if self.heartbeat_stale_seconds <= self.poll_seconds:
            raise ValueError("kb.worker.heartbeat_stale_seconds должен быть больше poll_seconds")
        return self


class KbTagsSettings(_Section):
    """Теги документов (docs/portal-api.md §8.5)."""

    max_per_document: int = Field(ge=1)
    max_length: int = Field(ge=1)
    max_selected: int = Field(ge=1)
    suggest_limit: int = Field(ge=1)


class KbImagesSettings(_Section):
    """Картинки документов и их описание моделью (docs/portal-api.md §8.6)."""

    enabled: bool
    min_side_px: int = Field(ge=1)
    min_area_px: int = Field(ge=1)
    max_per_document: int = Field(ge=1)
    description_max_tokens: int = Field(ge=1)
    system_prompt: str = Field(min_length=1)


class KbContextSettings(_Section):
    """Пояснения к фрагментам — задание `context` (docs/portal-api.md §11.5)."""

    enabled: bool
    window_tokens: int = Field(ge=1)
    max_tokens: int = Field(ge=1)
    max_chars: int = Field(ge=1)
    batch_size: int = Field(ge=1)
    system_prompt: str = Field(min_length=1)


class KbRewriteSettings(_Section):
    """Переписывание уточняющего вопроса в запрос для поиска (docs/portal-api.md §8.4)."""

    enabled: bool
    history_messages: int = Field(ge=1)
    history_max_chars: int = Field(ge=1)
    max_tokens: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)
    system_prompt: str = Field(min_length=1)


class KbRerankSettings(_Section):
    """Реранкер (docs/portal-api.md §8.4, §12.1)."""

    enabled: bool
    url: str = Field(min_length=1)
    model: str = Field(min_length=1)
    candidates: int = Field(ge=1)
    document_max_chars: int = Field(ge=1)
    timeout_seconds: float = Field(gt=0)


class KbSettings(_Section):
    """База знаний: загрузка, индексация, поиск, очередь."""

    document_max_bytes: int = Field(ge=1)
    document_max_pages: int = Field(ge=1)
    document_extensions: tuple[str, ...]
    context_max_tokens: int = Field(ge=1)
    rules_prompt: str = Field(min_length=1)
    empty_rules_prompt: str = Field(min_length=1)
    indexing: KbIndexingSettings
    chunking: KbChunkingSettings
    embeddings: KbEmbeddingsSettings
    search: KbSearchSettings
    qdrant: KbQdrantSettings
    worker: KbWorkerSettings
    tags: KbTagsSettings
    images: KbImagesSettings
    context: KbContextSettings
    rewrite: KbRewriteSettings
    rerank: KbRerankSettings

    @property
    def search_text_max_chars(self) -> int:
        """Наибольшая длина текста для поиска (§10.1): название, раздел, пояснение, фрагмент."""
        return (
            2 * self.chunking.section_max_chars
            + self.context.max_chars
            + self.chunking.max_chars
            + _SEARCH_TEXT_LABELS_CHARS
        )

    @model_validator(mode="after")
    def _check_search_text_fits(self) -> Self:
        """Ни эмбеддинги, ни реранкер не получают текст с обрезанным хвостом фрагмента."""
        if self.search_text_max_chars > self.embeddings.max_input_chars:
            raise ValueError("текст для поиска длиннее kb.embeddings.max_input_chars")
        if self.search_text_max_chars > self.rerank.document_max_chars:
            raise ValueError("текст для поиска длиннее kb.rerank.document_max_chars")
        return self

    @model_validator(mode="after")
    def _check_candidates(self) -> Self:
        if not self.search.prefetch_limit >= self.rerank.candidates >= self.search.top_k:
            raise ValueError(
                "нужно kb.search.prefetch_limit ≥ kb.rerank.candidates ≥ kb.search.top_k"
            )
        return self


class TemplateField(_Section):
    """Реквизит шаблона разбора."""

    name: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    title: str = Field(min_length=1)
    description: str = ""


class DocparseTemplate(_Section):
    """Шаблон разбора: либо список полей, либо произвольная форма."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = ""
    free_form: bool = False
    fields: tuple[TemplateField, ...] = ()

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.free_form == bool(self.fields):
            raise ValueError(f"шаблон {self.id}: нужен либо список fields, либо free_form: true")
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"шаблон {self.id}: имена полей повторяются")
        return self


class DocparseSettings(_Section):
    """Разбор документов."""

    document_max_bytes: int = Field(ge=1)
    max_pages: int = Field(ge=1)
    document_extensions: tuple[str, ...]
    max_output_tokens: int = Field(ge=1)
    dialog_reserve_tokens: int = Field(ge=0)
    run_timeout_seconds: int = Field(ge=1)
    free_form_max_items: int = Field(ge=1)
    reasoning_effort: ReasoningEffort
    extraction_system_prompt: str = Field(min_length=1)
    free_form_system_prompt: str = Field(min_length=1)
    summary_system_prompt: str = Field(min_length=1)
    dialog_system_prompt: str = Field(min_length=1)
    templates: tuple[DocparseTemplate, ...]

    def template(self, template_id: str) -> DocparseTemplate | None:
        """Шаблон по идентификатору."""
        return next((item for item in self.templates if item.id == template_id), None)

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Self:
        ids = [template.id for template in self.templates]
        if len(ids) != len(set(ids)):
            raise ValueError("docparse.templates: идентификаторы шаблонов повторяются")
        return self


class SqlDialect(_Section):
    """Диалект SQL в списке выбора."""

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    sqlglot: str = Field(min_length=1)


class SqlPrompts(_Section):
    """Системные сообщения SQL-помощника по действиям; `{dialect}` — название диалекта."""

    write: str = Field(min_length=1)
    explain: str = Field(min_length=1)
    debug: str = Field(min_length=1)
    optimize: str = Field(min_length=1)


class SqlSandboxSettings(_Section):
    """Проверка SQL на тестовых данных (docs/portal-api.md §7.4); пароли — из окружения."""

    enabled: bool
    dialect: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = Field(ge=1)
    database: str = Field(min_length=1)
    role_prefix: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    control_role: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    max_parallel: int = Field(ge=1)
    connect_timeout_seconds: float = Field(gt=0)
    statement_timeout_seconds: int = Field(ge=1)
    lock_timeout_seconds: int = Field(ge=1)
    total_timeout_seconds: int = Field(ge=1)
    result_max_rows: int = Field(ge=1)
    result_max_bytes: int = Field(ge=1)
    cell_max_chars: int = Field(ge=1)
    fixture_max_rows: int = Field(ge=1)
    fixture_max_bytes: int = Field(ge=1)
    error_max_chars: int = Field(ge=1)
    max_blocks: int = Field(ge=1)
    fix_attempts: int = Field(ge=0)
    max_output_tokens: int = Field(ge=1)
    data_system_prompt: str = Field(min_length=1)
    fix_system_prompt: str = Field(min_length=1)


class SqlSettings(_Section):
    """SQL-помощник."""

    dialects: tuple[SqlDialect, ...] = Field(min_length=1)
    default_dialect: str
    schema_max_chars: int = Field(ge=1)
    max_schemas: int = Field(ge=1)
    reasoning_effort: ReasoningEffort
    max_output_tokens: int = Field(ge=1)
    check_max_chars: int = Field(ge=1)
    check_timeout_seconds: float = Field(gt=0)
    error_line_patterns: tuple[str, ...]
    error_line_check_chars: int = Field(ge=1)

    @field_validator("error_line_patterns")
    @classmethod
    def _check_patterns(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(
                    "sql.error_line_patterns: негодное регулярное выражение"
                ) from error
        return patterns

    system_prompts: SqlPrompts
    sandbox: SqlSandboxSettings

    def dialect(self, dialect_id: str) -> SqlDialect | None:
        """Диалект по идентификатору."""
        return next((item for item in self.dialects if item.id == dialect_id), None)

    @model_validator(mode="after")
    def _check_default(self) -> Self:
        ids = {dialect.id for dialect in self.dialects}
        if self.default_dialect not in ids:
            raise ValueError("sql.default_dialect нет в sql.dialects")
        if self.sandbox.dialect not in ids:
            raise ValueError("sql.sandbox.dialect нет в sql.dialects")
        return self


class CogisPrompts(_Section):
    """Системные сообщения помощника CoGIS по действиям."""

    write: str = Field(min_length=1)
    explain: str = Field(min_length=1)
    debug: str = Field(min_length=1)


class CogisSettings(_Section):
    """Помощник по плагинам CoGIS."""

    reasoning_effort: ReasoningEffort
    max_output_tokens: int = Field(ge=1)
    system_prompts: CogisPrompts
    no_documentation_notice: str = Field(min_length=1)


class MediaFfmpegSettings(_Section):
    """Пределы одного запуска ffmpeg и ffprobe (docs/portal-api.md §8.7)."""

    timeout_seconds: int = Field(ge=1)
    memory_bytes: int = Field(ge=1)
    threads: int = Field(ge=1)
    output_max_bytes: int = Field(ge=1)


class MediaAsrSettings(_Section):
    """Распознавание речи на CPU (docs/portal-api.md §8.7)."""

    engine: Literal["gigaam", "stub"]
    model_dir: Path
    threads: int = Field(ge=1)
    chat_threads: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_threads(self) -> Self:
        if self.chat_threads >= self.threads:
            raise ValueError("media.asr.chat_threads должен быть меньше media.asr.threads")
        return self


class MediaSettings(_Section):
    """Аудио и видео — «записи» (docs/portal-api.md §1.6, §8.7).

    Параметры приёма объявляет `backend-engineer` (`app.yaml`), параметры обработки —
    `rag-engineer` (`kb.yaml`), §13.1.
    """

    enabled: bool
    max_bytes: int = Field(ge=1)
    max_minutes: int = Field(ge=1)
    chat_max_minutes: int = Field(ge=1)
    extensions: tuple[str, ...]
    quota_bytes: int = Field(ge=1)
    user_quota_share: float = Field(gt=0, le=1)
    min_free_bytes: int = Field(ge=0)
    max_concurrent_uploads: int = Field(ge=1)
    upload_idle_timeout_seconds: float = Field(gt=0)
    upload_timeout_seconds: float = Field(gt=0)
    audio_codecs: tuple[str, ...] = Field(min_length=1)
    failed_source_ttl_hours: int = Field(ge=1)
    part_seconds: int = Field(ge=1)
    audio_bitrate_kbps: int = Field(ge=1)
    ffmpeg: MediaFfmpegSettings
    asr: MediaAsrSettings


class Settings(_Section):
    """Все настройки приложения: YAML и секреты окружения."""

    server: ServerSettings
    database: DatabaseSettings
    auth: AuthSettings
    llm: LlmSettings
    files: FilesSettings
    dialogs: DialogsSettings
    chat: ChatSettings
    kb: KbSettings
    docparse: DocparseSettings
    sql: SqlSettings
    cogis: CogisSettings
    media: MediaSettings

    common_passwords: frozenset[str] = Field(repr=False)
    db_password: SecretStr
    secret_key: SecretBytes | None
    llm_api_key: SecretStr | None
    qdrant_api_key: SecretStr | None
    # Только для запасной схемы доступа к реранкеру (§12.1); в основной не задаётся.
    rerank_api_key: SecretStr | None = None
    max_model_len: int | None
    sql_sandbox_password: SecretStr | None = None
    sql_sandbox_control_password: SecretStr | None = None

    def require_llm_api_key(self) -> str:
        """Вернуть ключ `PORTAL_LLM_API_KEY` или отказать, если он не задан или пуст."""
        if self.llm_api_key is None:
            raise ConfigError("Не задана переменная окружения PORTAL_LLM_API_KEY")
        return self.llm_api_key.get_secret_value()

    def require_rerank_api_key(self) -> str:
        """Ключ реранкера: `PORTAL_RERANK_API_KEY`, а если она не задана — ключ портала."""
        if self.rerank_api_key is not None:
            return self.rerank_api_key.get_secret_value()
        return self.require_llm_api_key()

    def require_qdrant_api_key(self) -> str:
        """Вернуть ключ `QDRANT_API_KEY` или отказать, если он не задан или пуст."""
        if self.qdrant_api_key is None:
            raise ConfigError("Не задана переменная окружения QDRANT_API_KEY")
        return self.qdrant_api_key.get_secret_value()

    def require_sql_sandbox_passwords(self) -> tuple[str, str]:
        """Пароли ролей проверки и управляющей роли песочницы SQL (§13.5).

        Нужны только при включённом `sql.sandbox.enabled`; совпадать не должны.
        """
        run, control = self.sql_sandbox_password, self.sql_sandbox_control_password
        if run is None:
            raise ConfigError("Не задана переменная окружения SQL_SANDBOX_PASSWORD")
        if control is None:
            raise ConfigError("Не задана переменная окружения SQL_SANDBOX_CONTROL_PASSWORD")
        if run.get_secret_value() == control.get_secret_value():
            raise ConfigError(
                "SQL_SANDBOX_PASSWORD и SQL_SANDBOX_CONTROL_PASSWORD не должны совпадать"
            )
        return run.get_secret_value(), control.get_secret_value()

    def require_max_model_len(self) -> int:
        """Вернуть контекст модели `MAX_MODEL_LEN`; запасного значения нет (§13.5)."""
        if self.max_model_len is None:
            raise ConfigError("Не задана переменная окружения MAX_MODEL_LEN")
        return self.max_model_len

    @model_validator(mode="after")
    def _check_page_raster_fits(self) -> Self:
        """Растр страницы PDF ограничен стороной, а не числом точек: пределы согласованы."""
        if self.llm.image_max_side_px**2 > self.files.image_max_pixels:
            raise ValueError(
                "llm.image_max_side_px в квадрате не должен превышать files.image_max_pixels"
            )
        return self

    def require_secret_key(self) -> bytes:
        """Вернуть ключ `PORTAL_SECRET_KEY` или отказать, если он не задан."""
        if self.secret_key is None:
            raise ConfigError("Не задана переменная окружения PORTAL_SECRET_KEY")
        return self.secret_key.get_secret_value()


def _merge(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(content, dict):
        raise ConfigError(f"{path}: на верхнем уровне ожидается словарь")
    return content


def _decode_secret_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except ValueError as error:  # binascii.Error — её подкласс
        raise ConfigError("PORTAL_SECRET_KEY: ожидается стандартный base64") from error
    if len(key) != _SECRET_KEY_BYTES:
        raise ConfigError("PORTAL_SECRET_KEY: ожидается 32 байта (openssl rand -base64 32)")
    return key


def _parse_max_model_len(value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdecimal() or int(value) < 1:
        raise ConfigError("MAX_MODEL_LEN: ожидается положительное целое число")
    return int(value)


def _read_common_passwords(path: Path) -> frozenset[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return frozenset(line.lower() for line in lines if line)


def load_settings(
    environ: Mapping[str, str],
    config_dir: Path = CONFIG_DIR,
    override_path: Path = OVERRIDE_PATH,
) -> Settings:
    """Собрать настройки из каталога YAML, файла-накладки и переменных окружения."""
    files = sorted(config_dir.glob("*.yaml"))
    if not files:
        raise ConfigError(f"В каталоге {config_dir.resolve()} нет файлов конфигурации")
    data: dict[str, Any] = {}
    for path in files:
        data = _merge(data, _read_yaml(path))
    if override_path.is_file():
        data = _merge(data, _read_yaml(override_path))

    db_password = environ.get("PORTAL_DB_PASSWORD")
    if db_password is None:
        raise ConfigError("Не задана переменная окружения PORTAL_DB_PASSWORD")
    secret_key = environ.get("PORTAL_SECRET_KEY")
    llm_api_key = environ.get("PORTAL_LLM_API_KEY", "").strip()
    qdrant_api_key = environ.get("QDRANT_API_KEY", "").strip()
    rerank_api_key = environ.get("PORTAL_RERANK_API_KEY", "").strip()
    sandbox_password = environ.get("SQL_SANDBOX_PASSWORD", "")
    sandbox_control_password = environ.get("SQL_SANDBOX_CONTROL_PASSWORD", "")

    try:
        passwords_file = str(data["auth"]["password"]["common_passwords_file"])
    except (KeyError, TypeError) as error:
        raise ConfigError("В конфигурации нет auth.password.common_passwords_file") from error
    try:
        return Settings(
            **data,
            common_passwords=_read_common_passwords(config_dir / passwords_file),
            db_password=SecretStr(db_password),
            secret_key=SecretBytes(_decode_secret_key(secret_key)) if secret_key else None,
            llm_api_key=SecretStr(llm_api_key) if llm_api_key else None,
            qdrant_api_key=SecretStr(qdrant_api_key) if qdrant_api_key else None,
            rerank_api_key=SecretStr(rerank_api_key) if rerank_api_key else None,
            max_model_len=_parse_max_model_len(environ.get("MAX_MODEL_LEN")),
            sql_sandbox_password=SecretStr(sandbox_password) if sandbox_password else None,
            sql_sandbox_control_password=(
                SecretStr(sandbox_control_password) if sandbox_control_password else None
            ),
        )
    except ValueError as error:
        raise ConfigError(f"Ошибка конфигурации: {error}") from error
