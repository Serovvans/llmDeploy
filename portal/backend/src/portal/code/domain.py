"""Сущности раздела «Код» (docs/portal-api.md §7.5.1, §9.23–9.27)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

CodeAction = Literal["write", "explain", "fix"]
BuildStatus = Literal["succeeded", "failed", "timeout"]
SnapshotReason = Literal["edit_accepted", "build_succeeded", "restored", "before_restore"]
EditStatus = Literal["proposed", "accepted", "rejected", "superseded"]
EditChange = Literal["modified", "added"]


@dataclass
class Project:
    """Проект сотрудника; `revision` растёт при каждом изменении файлов и пакетов (§7.5.3)."""

    id: UUID
    owner_id: UUID
    name: str
    packages: list[str]
    schema_id: UUID | None
    revision: int
    created_at: datetime
    updated_at: datetime


@dataclass
class ProjectFile:
    """Файл проекта; `revision` — номер изменения файла, с 1."""

    id: UUID
    project_id: UUID
    path: str
    content: str
    size_bytes: int
    revision: int
    updated_at: datetime


@dataclass(frozen=True)
class Snapshot:
    """Версия проекта: все файлы и ревизия проекта, при которой они сняты (§7.5.6)."""

    id: UUID
    project_id: UUID
    reason: SnapshotReason
    files: list[dict[str, str]]
    project_revision: int
    accepted_files: int | None
    restored_from: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class StoredBuild:
    """Последняя сборка кнопкой (§9.26) без содержимого архива.

    `rules` — `{factory, tools, findings}` только при `succeeded`; `archive_files` —
    `CodeBuild.archive`, есть вместе с архивом.
    """

    project_id: UUID
    status: BuildStatus
    project_revision: int
    sdk_version: str | None
    packages: list[dict[str, str]]
    diagnostics: list[dict[str, Any]]
    diagnostics_truncated: bool
    rules: dict[str, Any] | None
    archive_files: dict[str, Any] | None
    created_at: datetime

    @property
    def findings(self) -> list[dict[str, Any]] | None:
        """Находки правил; `None` — правила не проверялись."""
        return None if self.rules is None else list(self.rules.get("findings") or [])

    @property
    def has_problems(self) -> bool:
        """Есть ли ошибки компиляции или находки правил — есть что исправлять (§7.5.8)."""
        errors = any(item.get("severity") == "error" for item in self.diagnostics)
        return errors or bool(self.findings)


@dataclass(frozen=True)
class EditFile:
    """Файл правки помощника: предлагаемое содержимое и базовая ревизия (§9.27)."""

    path: str
    change: EditChange
    content: str
    base_revision: int | None


@dataclass(frozen=True)
class ParsedEdit:
    """Итог разбора ответа модели (§7.5.10): файлы, отказы и признак обрыва."""

    files: list[EditFile]
    rejected: list[dict[str, str]]
    incomplete: bool


@dataclass(frozen=True)
class CodeLimits:
    """Пределы проекта (§7.5.3)."""

    max_files: int
    file_max_bytes: int
    project_max_bytes: int
    extensions: tuple[str, ...]


@dataclass(frozen=True)
class ProjectView:
    """Проект с файлами для сборки запроса помощника и разбора правки."""

    name: str
    files: list[ProjectFile] = field(default_factory=list)


@dataclass(frozen=True)
class ReferenceMember:
    """Член типа справочника API (§7.5.14)."""

    kind: str
    name: str
    signature: str


@dataclass(frozen=True)
class ReferenceType:
    """Тип справочника API: объявление и открытые и защищённые члены (§7.5.14)."""

    namespace: str
    name: str
    kind: str
    origin: str
    signature: str
    members: tuple[ReferenceMember, ...]

    @property
    def full_name(self) -> str:
        """Полное имя `<namespace>.<name>`."""
        return f"{self.namespace}.{self.name}" if self.namespace else self.name


@dataclass(frozen=True)
class TargetView:
    """Выделенный фрагмент вопроса: снимок текста на момент вопроса (§7.5.8)."""

    file_id: str
    path: str
    start_line: int
    end_line: int
    text: str


@dataclass(frozen=True)
class SchemaView:
    """Схема SQL проекта для помощника."""

    name: str
    content: str


@dataclass(frozen=True)
class BuildView:
    """Сборка для блока `<сборка>`: итог, устарела ли и сообщения (§7.5.9)."""

    status: BuildStatus
    outdated: bool
    diagnostics: tuple[dict[str, Any], ...]
    findings: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SdkSnapshot:
    """Состояние индекса: тело `CodeSdk`, его `ETag`, типы для помощника и ссылки выгрузки.

    `types` — `None`, когда справочника нет (в системном сообщении — `no_sdk_notice`);
    `references` — DLL для `Reference` выгрузки, кроме вида `framework`; `libraries` —
    имена DLL сервера CoGIS (папка SDK и `plugins.txt`).
    """

    available: bool
    sdk_version: str | None
    types: tuple[ReferenceType, ...] | None
    references: tuple[str, ...]
    libraries: tuple[str, ...]
    body: bytes
    etag: str


@dataclass
class ImportResult:
    """Взятые файлы, пакеты проекта и отчёт `ImportReport`."""

    files: list[tuple[str, str]] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    ignored_files: int = 0
    skipped_files: list[dict[str, str]] = field(default_factory=list)
    skipped_total: int = 0
    unmatched_packages: list[dict[str, str | None]] = field(default_factory=list)

    def report(self) -> dict[str, Any]:
        """Объект `ImportReport` (§7.5.5)."""
        return {
            "ignored_files": self.ignored_files,
            "skipped_files": self.skipped_files,
            "skipped_total": self.skipped_total,
            "unmatched_packages": self.unmatched_packages,
        }


@dataclass(frozen=True)
class CogisArchive:
    """Архив для CoGIS и его состав `CodeBuild.archive` (§7.5.1)."""

    content: bytes
    files: dict[str, Any]
