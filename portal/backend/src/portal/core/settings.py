"""Единственное место чтения конфигурации: YAML, файл-накладка и секреты из окружения.

Порядок (docs/portal-api.md §13.5): все `*.yaml` каталога конфигурации объединяются,
поверх накладывается необязательный файл-накладка; словари объединяются по ключам,
списки и простые значения заменяются целиком.
"""

import base64
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretBytes, SecretStr, model_validator

CONFIG_DIR = Path("config")
OVERRIDE_PATH = Path("/etc/portal/config.override.yaml")

_SECRET_KEY_BYTES = 32


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

    @model_validator(mode="after")
    def _check_heartbeat(self) -> Self:
        if self.heartbeat_stale_seconds <= self.poll_seconds:
            raise ValueError("kb.worker.heartbeat_stale_seconds должен быть больше poll_seconds")
        return self


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

    @model_validator(mode="after")
    def _check_fragment_fits_embedder(self) -> Self:
        if self.chunking.max_chars > self.embeddings.max_input_chars:
            raise ValueError("kb.chunking.max_chars больше kb.embeddings.max_input_chars")
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
    templates: tuple[DocparseTemplate, ...]

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


class SqlSettings(_Section):
    """SQL-помощник."""

    dialects: tuple[SqlDialect, ...] = Field(min_length=1)
    default_dialect: str
    schema_max_chars: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_default(self) -> Self:
        if self.default_dialect not in {dialect.id for dialect in self.dialects}:
            raise ValueError("sql.default_dialect нет в sql.dialects")
        return self


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

    common_passwords: frozenset[str] = Field(repr=False)
    db_password: SecretStr
    secret_key: SecretBytes | None
    llm_api_key: SecretStr | None
    qdrant_api_key: SecretStr | None
    max_model_len: int | None

    def require_llm_api_key(self) -> str:
        """Вернуть ключ `PORTAL_LLM_API_KEY` или отказать, если он не задан или пуст."""
        if self.llm_api_key is None:
            raise ConfigError("Не задана переменная окружения PORTAL_LLM_API_KEY")
        return self.llm_api_key.get_secret_value()

    def require_qdrant_api_key(self) -> str:
        """Вернуть ключ `QDRANT_API_KEY` или отказать, если он не задан или пуст."""
        if self.qdrant_api_key is None:
            raise ConfigError("Не задана переменная окружения QDRANT_API_KEY")
        return self.qdrant_api_key.get_secret_value()

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
            max_model_len=_parse_max_model_len(environ.get("MAX_MODEL_LEN")),
        )
    except ValueError as error:
        raise ConfigError(f"Ошибка конфигурации: {error}") from error
