"""Порты раздела «Код»: песочница сборки и хранилище проектов (docs/portal-api.md §13.3).

Песочница — граница с сервисом `code-sandbox` (§7.5.12): всё, что уходит в неё и приходит
из неё, — данные чужого кода, сценарий их не исполняет. Каждый метод хранилища с данными
сотрудника принимает владельца и включает условие `code_projects.owner_id = :owner` в сам
запрос (§9.15).
"""

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID

from portal.code.domain import (
    BuildStatus,
    CodeLimits,
    CogisArchive,
    ImportResult,
    Project,
    ProjectFile,
    SdkSnapshot,
    Snapshot,
    StoredBuild,
)
from portal.core.settings import CodePackage
from portal.dialogs.ports import DialogRepository


@dataclass(frozen=True)
class Diagnostic:
    """Сообщение компилятора (§7.5.1): строки и столбцы с 1, `file = None` — без места."""

    severity: Literal["error", "warning"]
    code: str
    file: str | None
    line: int | None
    column: int | None
    end_line: int | None
    end_column: int | None
    message: str

    def data(self) -> dict[str, Any]:
        """Объект `Diagnostic` контракта."""
        return asdict(self)


@dataclass(frozen=True)
class RuleFinding:
    """Находка правил GPTool (§7.5.13); место — `None` у находки без места."""

    rule: str
    file: str | None
    line: int | None
    column: int | None
    message: str

    def data(self) -> dict[str, Any]:
        """Объект `RuleFinding` контракта."""
        return asdict(self)


@dataclass(frozen=True)
class RuleTool:
    """Инструмент проекта по атрибутам: полное имя класса, название и категория."""

    name: str
    title: str | None
    category: str | None


@dataclass(frozen=True)
class BuildRules:
    """Итог правил GPTool — только у собранного проекта."""

    factory: str | None
    tools: Sequence[RuleTool]
    findings: Sequence[RuleFinding]

    def data(self) -> dict[str, Any]:
        """Поля `{factory, tools, findings}` для хранения у сборки (§9.26)."""
        return {
            "factory": self.factory,
            "tools": [asdict(tool) for tool in self.tools],
            "findings": [finding.data() for finding in self.findings],
        }


@dataclass(frozen=True)
class CompileFile:
    """Файл проекта для сборки."""

    path: str
    content: str


@dataclass(frozen=True)
class CompileResult:
    """Ответ песочницы на сборку (§7.5.12)."""

    status: BuildStatus
    sdk_version: str | None
    diagnostics: Sequence[Diagnostic]
    diagnostics_truncated: bool
    rules: BuildRules | None
    dll: bytes | None
    bundle: Sequence[tuple[str, bytes]]
    on_server: Sequence[str]

    @property
    def errors(self) -> list[Diagnostic]:
        """Ошибки компиляции."""
        return [item for item in self.diagnostics if item.severity == "error"]

    @property
    def warnings(self) -> int:
        """Число предупреждений."""
        return sum(item.severity == "warning" for item in self.diagnostics)

    @property
    def findings(self) -> list[RuleFinding]:
        """Находки правил; пусто, если правила не проверялись."""
        return [] if self.rules is None else list(self.rules.findings)


class BuildInProgressError(Exception):
    """У проекта уже идёт сборка кнопкой (§7.5.7, шаг 2)."""


class BuildUnavailableError(Exception):
    """Собрать негде: не дождались места, песочница отказала или ответила не по протоколу."""


class CodeSandboxSlot(Protocol):
    """Занятое место сборки."""

    async def compile(
        self,
        assembly_name: str,
        files: Sequence[CompileFile],
        packages: Sequence[tuple[str, str]],
    ) -> CompileResult:
        """Собрать проект; `packages` — `(id, version)` уже с зависимостями.

        Общий срок `code.build.timeout_seconds` — `status = "timeout"`. Отказ, обрыв,
        4xx/5xx, ответ не по протоколу или больше `code.build.response_max_bytes` —
        `BuildUnavailableError`. Отмена задачи обрывает запрос.
        """
        ...


class CodeSandbox(Protocol):
    """Песочница сборки: одна сборка проекта за раз, на портал — `code.build.max_parallel`."""

    def slot(
        self, project_id: UUID, *, button: bool
    ) -> AbstractAsyncContextManager[CodeSandboxSlot]:
        """Дождаться своей очереди в проекте и места (§7.5.7, шаги 2 и 4).

        `button=True`: у проекта уже идёт сборка кнопкой — сразу `BuildInProgressError`,
        идущую сборку цикла — ждёт. `button=False` (цикл исправления) ждёт любую идущую.
        Всё ожидание — не дольше `code.build.wait_seconds`, иначе `BuildUnavailableError`.
        """
        ...

    def running(self, project_id: UUID) -> bool:
        """Идёт ли сборка проекта — от входа в `slot` до выхода, включая ожидание."""
        ...


@dataclass(frozen=True)
class ProjectPage:
    """Строка списка проектов с итогом последней сборки для значка (§7.5.1)."""

    project: Project
    build: StoredBuild | None


@dataclass(frozen=True)
class StoredEdit:
    """Правка помощника: сводка `messages.code_edit` и содержимое `code_edit_files`."""

    message_id: UUID
    summary: dict[str, Any]
    files: Mapping[str, tuple[int | None, str]]  # path → (base_revision, content)


class CodeRepository(Protocol):
    """Проекты, файлы, версии, сборки и правки помощника владельца."""

    async def lock_owner(self, owner_id: UUID) -> None:
        """Блокировка строки пользователя: создание и импорт не обходят предел проектов."""
        ...

    async def count_projects(self, owner_id: UUID) -> int:
        """Число проектов сотрудника."""
        ...

    async def name_taken(self, owner_id: UUID, name: str) -> bool:
        """Есть ли у сотрудника проект с этим именем без учёта регистра."""
        ...

    async def add_project(self, project: Project, files: Sequence[ProjectFile]) -> None:
        """Создать проект с файлами."""
        ...

    async def list_projects(
        self, owner_id: UUID, limit: int, before: tuple[datetime, UUID] | None
    ) -> list[ProjectPage]:
        """Проекты по убыванию `updated_at`, затем `id`, после курсора."""
        ...

    async def get_project(
        self, owner_id: UUID, project_id: UUID, *, lock: bool = False
    ) -> Project | None:
        """Проект владельца; `lock` — держать строку до конца транзакции."""
        ...

    async def project_by_dialog(self, owner_id: UUID, dialog_id: UUID) -> Project | None:
        """Проект, которому принадлежит диалог помощника."""
        ...

    async def dialog_of(self, owner_id: UUID, project_id: UUID) -> UUID | None:
        """Диалог помощника проекта."""
        ...

    async def save_project(self, project: Project) -> None:
        """Записать пакеты, схему, ревизию и время изменения проекта."""
        ...

    async def delete_project(self, owner_id: UUID, project_id: UUID) -> bool:
        """Удалить проект каскадом; `False` — его нет."""
        ...

    async def files(self, owner_id: UUID, project_id: UUID) -> list[ProjectFile]:
        """Файлы проекта по пути без учёта регистра."""
        ...

    async def add_file(self, owner_id: UUID, file: ProjectFile) -> None:
        """Создать файл."""
        ...

    async def save_file(self, owner_id: UUID, file: ProjectFile) -> None:
        """Записать путь, содержимое, размер, ревизию и время файла."""
        ...

    async def delete_file(self, owner_id: UUID, project_id: UUID, file_id: UUID) -> None:
        """Удалить файл."""
        ...

    async def snapshots(self, owner_id: UUID, project_id: UUID) -> list[Snapshot]:
        """Хранимые версии по ревизии, новые сверху."""
        ...

    async def get_snapshot(
        self, owner_id: UUID, project_id: UUID, snapshot_id: UUID
    ) -> Snapshot | None:
        """Версия проекта."""
        ...

    async def add_snapshot(self, owner_id: UUID, snapshot: Snapshot, keep: int) -> bool:
        """Создать версию, если с её ревизией версии нет; оставить `keep` новейших.

        `False` — снимок с этой ревизией уже есть, ничего не создано.
        """
        ...

    async def build(self, owner_id: UUID, project_id: UUID) -> StoredBuild | None:
        """Последняя сборка кнопкой без архива."""
        ...

    async def save_build(self, owner_id: UUID, build: StoredBuild, archive: bytes | None) -> None:
        """Заменить сборку проекта новой (с архивом или без)."""
        ...

    async def archive(self, owner_id: UUID, project_id: UUID) -> bytes | None:
        """Сохранённый архив для CoGIS."""
        ...

    async def edit(self, owner_id: UUID, project_id: UUID, message_id: UUID) -> StoredEdit | None:
        """Правка ответа диалога этого проекта."""
        ...

    async def save_edit(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        message_id: UUID,
        summary: Mapping[str, Any],
        files: Mapping[str, tuple[int | None, str]],
    ) -> bool:
        """Записать правку ответа и перевести прежние `proposed` диалога в `superseded`.

        `False` — ответа уже нет (диалог удалён).
        """
        ...

    async def decide_edit(
        self, owner_id: UUID, project_id: UUID, message_id: UUID, summary: Mapping[str, Any]
    ) -> None:
        """Записать решение по правке (сводка целиком)."""
        ...


class CodeUnitOfWork(Protocol):
    """Хранилища одной транзакции: проект и его диалог создаются вместе (§7.5.4)."""

    code: CodeRepository
    dialogs: DialogRepository


class CodeUnitOfWorkFactory(Protocol):
    """Открывает единицу работы: фиксация при выходе без ошибки, иначе откат."""

    def __call__(self) -> AbstractAsyncContextManager[CodeUnitOfWork]:
        """Начать транзакцию."""
        ...


class SdkReference(Protocol):
    """Индекс SDK CoGIS (§7.5.14): справочник, ссылки выгрузки и тело `GET /api/code/sdk`."""

    def snapshot(self) -> SdkSnapshot:
        """Текущее состояние индекса; может читать файлы — вызывается в потоке."""
        ...


class ProjectArchives(Protocol):
    """Архивы ZIP проекта: импорт, выгрузка, архив для CoGIS (§7.5.5, §7.5.7)."""

    def read_import(
        self,
        data: bytes,
        limits: CodeLimits,
        packages: Sequence[CodePackage],
        max_directory_bytes: int,
    ) -> ImportResult:
        """Файлы `.cs` и пакеты из архива; отказы — ошибки API §7.5.5."""
        ...

    def export_zip(
        self,
        name: str,
        files: Sequence[tuple[str, str]],
        references: Sequence[str],
        packages: Sequence[tuple[str, str]],
        memo: str,
        moment: datetime,
    ) -> bytes:
        """Проект в папке `<name>/` с `.csproj` и памяткой."""
        ...

    def web_zip(self, files: Sequence[tuple[str, str]], memo: str, moment: datetime) -> bytes:
        """Выгрузка веб-вида: файлы по путям и `Памятка.txt` (§7.5.16)."""
        ...

    def cogis_archive(
        self,
        name: str,
        dll: bytes,
        bundle: Sequence[tuple[str, bytes]],
        on_server: Sequence[str],
        factory: str | None,
        tools: Sequence[tuple[str, str | None]],
        sdk_version: str | None,
        memo: str,
        moment: datetime,
    ) -> CogisArchive:
        """DLL инструмента, DLL пакетов и памятка по установке."""
        ...
