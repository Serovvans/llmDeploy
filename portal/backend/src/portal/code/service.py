"""Сценарии раздела «Код»: проекты, файлы, версии, сборка, архив и правки (§7.5).

Каждое изменение файлов и пакетов идёт под блокировкой строки проекта, создание и импорт —
под блокировкой строки пользователя: одновременные запросы пределы не обходят (§7.5.3).
"""

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC
from typing import Any, Literal
from uuid import UUID, uuid4

from portal.code import errors
from portal.code.domain import (
    CodeLimits,
    Project,
    ProjectFile,
    SdkSnapshot,
    Snapshot,
    StoredBuild,
    WebAssets,
)
from portal.code.kinds import (
    CLIENTS,
    WEB_KINDS,
    Kind,
    default_catalog,
    kind_limits,
    kinds,
    web_memo,
    web_name_error,
)
from portal.code.paths import (
    conflicts,
    content_error,
    limit_exceeded,
    path_error,
    project_name_error,
    utf8_size,
)
from portal.code.ports import (
    BuildInProgressError,
    BuildUnavailableError,
    CodeSandbox,
    CodeUnitOfWork,
    CodeUnitOfWorkFactory,
    CompileFile,
    CompileResult,
    ProjectArchives,
    ProjectPage,
    SdkReference,
)
from portal.code.wizard import Scaffold, WizardTemplates, check_form, example, scaffold
from portal.core.errors import feature_disabled, field_error, not_found, validation_error
from portal.core.ports import Clock, CurrentUser
from portal.core.settings import CodeSettings
from portal.databases import errors as db_errors
from portal.databases.domain import Selection, selection_of
from portal.databases.service import DbService, SelectionInput
from portal.dialogs.domain import Dialog
from portal.dialogs.service import (
    CarriedInput,
    CursorPage,
    carried_fields,
    decode_time_cursor,
    encode_time_cursor,
)
from portal.tools import errors as tool_errors
from portal.tools.sql_schemas import SqlSchemaService

logger = logging.getLogger(__name__)

_FIELD_MESSAGES = {
    "outside_project": "Путь ведёт за пределы проекта.",
    "extension_not_allowed": "Недопустимое расширение файла.",
    "path_conflict": "Путь совпадает с папкой другого файла.",
}


class _Unset:
    """Поле не передано: `schema_id: null` — осмысленный выбор «без схемы»."""


UNSET = _Unset()


@dataclass(frozen=True)
class CatalogInput:
    """Настройки кастомизации файла в `catalog_files` (§7.5.16)."""

    file_id: UUID
    insertion: str
    clients: list[str]


def _field(name: str, code: str) -> Any:
    return field_error(name, code, _FIELD_MESSAGES.get(code))


def check_path(path: str, extensions: Sequence[str], field: str = "path") -> None:
    """Путь файла по §7.5.3; нарушение — `validation_error` по полю."""
    if (code := path_error(path, extensions)) is not None:
        raise validation_error([_field(field, code)])


def check_name(name: str, packages: Sequence[str]) -> None:
    """Имя проекта по §7.5.3; `packages` — `id` пакетов набора."""
    if project_name_error(name, packages) is not None:
        raise validation_error([field_error("name", "invalid_format")])


def with_dependencies(selected: Sequence[str], settings: CodeSettings) -> list[tuple[str, str]]:
    """Пакеты сборки: выбранные и их зависимости по цепочке, в порядке набора (§7.5.7)."""
    wanted: set[str] = set()
    pending = [item.lower() for item in selected]
    while pending:
        name = pending.pop()
        if name in wanted:
            continue
        wanted.add(name)
        package = settings.package(name)
        if package is not None:
            pending.extend(item.lower() for item in package.depends_on)
    return [(item.id, item.version) for item in settings.packages if item.id.lower() in wanted]


@dataclass(frozen=True)
class ProjectDetails:
    """Проект целиком для `CodeProject` (§7.5.1)."""

    project: Project
    files: list[ProjectFile]
    build: StoredBuild | None
    dialog_id: UUID
    running: bool

    @property
    def outdated(self) -> bool:
        """Сборка устарела: ревизия проекта не та, с которой собрано."""
        return self.build is not None and self.build.project_revision != self.project.revision


@dataclass(frozen=True)
class Created:
    """Ответ создания проекта: проект, запрос помощнику мастера, отчёт импорта."""

    details: ProjectDetails
    assistant_request: str | None
    report: dict[str, Any] | None


@dataclass(frozen=True)
class BuildDetails:
    """Итог сборки кнопкой и его актуальность."""

    build: StoredBuild
    outdated: bool


@dataclass(frozen=True)
class EditDetails:
    """Правка целиком для `CodeEditFull` (§7.5.1)."""

    summary: dict[str, Any]
    files: list[dict[str, Any]]


class CodeService:
    """Проекты сотрудника; чужое всегда `not_found`."""

    def __init__(
        self,
        uow_factory: CodeUnitOfWorkFactory,
        sandbox: CodeSandbox | None,
        sdk: SdkReference,
        archives: ProjectArchives,
        schemas: SqlSchemaService,
        templates: WizardTemplates,
        clock: Clock,
        settings: CodeSettings,
        *,
        web: WebAssets,
        databases: DbService | None,
        message_max_chars: int,
        carried_max_chars: int,
        zip_max_directory_bytes: int,
    ) -> None:
        """Получить зависимости явно.

        `sandbox` — `None`, когда сборка выключена флагом `code.build.enabled`: к песочнице
        портал не обращается (§7.5.15). `databases` — `None` у прогона контрольного набора:
        выбора подключения там нет.
        """
        self._uow_factory = uow_factory
        self._sandbox = sandbox
        self._sdk = sdk
        self._archives = archives
        self._schemas = schemas
        self._templates = templates
        self._clock = clock
        self._settings = settings
        self._message_max_chars = message_max_chars
        self._carried_max_chars = carried_max_chars
        self._zip_max_directory_bytes = zip_max_directory_bytes
        self._web = web
        self._databases = databases
        self.kinds: dict[str, Kind] = kinds(settings.file_extensions)
        self.limits = CodeLimits(
            settings.max_files,
            settings.file_max_bytes,
            settings.project_max_bytes,
            settings.file_extensions,
        )
        self._tasks: set[asyncio.Task[Any]] = set()

    async def shutdown(self) -> None:
        """Дождаться идущих сборок при остановке процесса не нужно: отменить их."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def limits_of(self, project: Project) -> CodeLimits:
        """Пределы проекта его вида: расширения и «один файл» (§7.5.16)."""
        return kind_limits(self.limits, self.kinds[project.kind])

    @property
    def web(self) -> WebAssets:
        """Шаблоны веб-видов и описание `api`."""
        return self._web

    @property
    def templates(self) -> WizardTemplates:
        """Шаблоны мастера: типы параметров отдаются интерфейсу (`GET /api/config`)."""
        return self._templates

    @property
    def sandbox(self) -> CodeSandbox | None:
        """Песочница сборки; `None` — сборка выключена флагом."""
        return self._sandbox

    def running(self, project_id: UUID) -> bool:
        """Идёт ли сборка проекта (`CodeProject.build_running`)."""
        return self._sandbox is not None and self._sandbox.running(project_id)

    async def sdk(self) -> SdkSnapshot:
        """Индекс SDK; файлы читаются в потоке."""
        return await asyncio.to_thread(self._sdk.snapshot)

    # --- проекты ---

    async def list_projects(
        self, owner_id: UUID, limit: int, cursor: str | None
    ) -> CursorPage[ProjectPage]:
        """Проекты по убыванию `updated_at`, затем `id` (§7.5.2)."""
        before = None if cursor is None else decode_time_cursor(cursor)
        async with self._uow_factory() as uow:
            items = await uow.code.list_projects(owner_id, limit + 1, before)
        more = len(items) > limit
        items = items[:limit]
        last = items[-1].project if items else None
        next_cursor = encode_time_cursor(last.updated_at, last.id) if more and last else None
        return CursorPage(items, next_cursor)

    async def _details(
        self, uow: CodeUnitOfWork, owner_id: UUID, project: Project
    ) -> ProjectDetails:
        files = await uow.code.files(owner_id, project.id)
        build = await uow.code.build(owner_id, project.id)
        dialog_id = await uow.code.dialog_of(owner_id, project.id)
        if dialog_id is None:
            raise not_found()
        return ProjectDetails(project, files, build, dialog_id, self.running(project.id))

    async def get(self, owner_id: UUID, project_id: UUID) -> ProjectDetails:
        """Проект с файлами, последней сборкой и диалогом помощника."""
        async with self._uow_factory() as uow:
            return await self._details(
                uow, owner_id, await self._project(uow, owner_id, project_id)
            )

    async def project(self, owner_id: UUID, project_id: UUID) -> Project:
        """Свой проект без файлов; чужой — `not_found`."""
        async with self._uow_factory() as uow:
            return await self._project(uow, owner_id, project_id)

    async def preview_layer(
        self, owner_id: UUID, project_id: UUID, schema: str, table: str
    ) -> dict[str, Any]:
        """Слой предпросмотра из образца отмеченной таблицы веб-проекта (§7.5.18)."""
        databases = self._databases
        if databases is None or not self._settings.preview.enabled or not databases.enabled:
            raise feature_disabled()
        project = await self.project(owner_id, project_id)
        if not project.web:
            raise errors.wrong_project_kind()
        selection = selection_of(
            project.db_connection_id, project.db_connection_title, project.db_tables
        )
        if selection is None:
            raise db_errors.schema_required()
        return await databases.preview(owner_id, selection, schema, table)

    async def _project(
        self, uow: CodeUnitOfWork, owner_id: UUID, project_id: UUID, *, lock: bool = False
    ) -> Project:
        project = await uow.code.get_project(owner_id, project_id, lock=lock)
        if project is None:
            raise not_found()
        return project

    async def _create(
        self,
        owner_id: UUID,
        name: str,
        built: Scaffold | list[tuple[str, str]],
        packages: Sequence[str],
        carried: CarriedInput | None = None,
        kind: str = "gptool",
    ) -> ProjectDetails:
        """Создать проект и его диалог одной транзакцией; версии не создаются (§7.5.4)."""
        sources = built.files if isinstance(built, Scaffold) else built
        now = self._clock.now()
        project = Project(uuid4(), owner_id, name, list(packages), None, 1, now, now, kind=kind)
        files = [self._new_file(project, path, content, now) for path, content in sources]
        limit = limit_exceeded(len(files), sum(file.size_bytes for file in files), self.limits)
        if limit is not None:
            raise errors.project_limit(limit, self._limit_value(limit))
        dialog = Dialog(uuid4(), owner_id, "code", None, now, now, project_id=project.id)
        async with self._uow_factory() as uow:
            await uow.code.lock_owner(owner_id)
            if await uow.code.count_projects(owner_id) >= self._settings.max_projects:
                raise errors.project_limit_reached(self._settings.max_projects)
            if await uow.code.name_taken(owner_id, name):
                raise errors.project_name_taken()
            if carried is not None:
                fields = await carried_fields(
                    uow.dialogs, owner_id, carried, self._carried_max_chars
                )
                dialog.carried_text = fields.text
                dialog.carried_source_id = fields.source_id
                dialog.carried_source_title = fields.source_title
            await uow.code.add_project(project, files)
            await uow.dialogs.add_dialog(dialog)
        files.sort(key=lambda file: file.path.lower())
        return ProjectDetails(project, files, None, dialog.id, False)

    def _new_file(self, project: Project, path: str, content: str, now: Any) -> ProjectFile:
        """Новый файл; у кастомизации Каталога — настройки по расширению (§7.5.16)."""
        file = ProjectFile(uuid4(), project.id, path, content, utf8_size(content), 1, now)
        if project.kind == "catalog":
            file.catalog_insertion, file.catalog_clients = default_catalog(path)
        return file

    @property
    def _package_ids(self) -> list[str]:
        """`id` пакетов набора в порядке `code.packages`."""
        return [item.id for item in self._settings.packages]

    def _limit_value(self, limit: Literal["files", "bytes"]) -> int:
        return self.limits.max_files if limit == "files" else self.limits.project_max_bytes

    async def create_wizard(
        self, owner_id: UUID, form: Mapping[str, Any], carried: CarriedInput | None
    ) -> Created:
        """Каркас мастера «Новый GPTool» (§7.5.4); с `carried` — перенос в «Код»."""
        check_form(
            form,
            self._templates,
            self._settings.wizard.max_tools,
            self._settings.wizard.max_parameters,
            self._message_max_chars,
            self._package_ids,
        )
        built = scaffold(
            form,
            self._templates,
            self._settings.prompts.wizard_request,
            self._package_ids,
        )
        if built.assistant_request is not None and (
            len(built.assistant_request) > self._message_max_chars
        ):
            raise validation_error([field_error("tools", "too_long")])
        details = await self._create(owner_id, form["name"], built, built.packages, carried)
        return Created(details, built.assistant_request, None)

    async def create_example(self, owner_id: UUID, name: str) -> Created:
        """Проект-пример по инструкции заказчика (§7.5.4)."""
        check_name(name, self._package_ids)
        built = example(name, self._templates, self._package_ids)
        return Created(await self._create(owner_id, name, built, built.packages), None, None)

    async def create_template(self, owner_id: UUID, kind: str, name: str) -> Created:
        """Проект веб-вида из шаблона: один файл, диалог помощника, без версий (§7.5.16)."""
        if kind not in WEB_KINDS:
            raise validation_error([field_error("kind", "unknown_value")])
        if (code := web_name_error(name)) is not None:
            raise validation_error([field_error("name", code)])
        template = self.kinds[kind].template
        assert template is not None  # у каждого веб-вида есть шаблон
        files = [(template, self._web.templates[template])]
        details = await self._create(owner_id, name.strip(), files, [], kind=kind)
        return Created(details, None, None)

    async def import_zip(self, owner_id: UUID, name: str, data: bytes) -> Created:
        """Проект из архива ZIP: только файлы `.cs` и пакеты набора (§7.5.5)."""
        check_name(name, self._package_ids)
        result = await asyncio.to_thread(
            self._archives.read_import,
            data,
            self.limits,
            self._settings.packages,
            self._zip_max_directory_bytes,
        )
        details = await self._create(owner_id, name, result.files, result.packages)
        return Created(details, None, result.report())

    async def patch(
        self,
        user: CurrentUser,
        project_id: UUID,
        packages: Sequence[str] | None,
        schema_id: UUID | _Unset | None,
        db: SelectionInput | _Unset | None = UNSET,
        catalog_files: Sequence[CatalogInput] | None = None,
    ) -> ProjectDetails:
        """Пакеты, схема, подключение и настройки Каталога проекта (§7.5.3, §7.5.16, §7.6.9).

        Схема и подключение взаимоисключающие: выбор одного снимает другое; `schema_id`
        снимает подключение и при выключенном `db.enabled`.
        """
        owner_id = user.id
        if isinstance(schema_id, UUID) and isinstance(db, SelectionInput):
            raise validation_error([field_error("db", "invalid_format")])
        if not isinstance(db, _Unset) and (self._databases is None or not self._databases.enabled):
            raise feature_disabled()
        if catalog_files is not None:
            self._check_catalog_input(catalog_files)
        chosen: list[str] | None = None
        if packages is not None:
            known = [self._settings.package(item) for item in packages]
            if any(item is None for item in known):
                raise validation_error([field_error("packages", "unknown_value")])
            ids = {item.id for item in known if item is not None}
            if len(ids) != len(packages):
                raise validation_error([field_error("packages", "invalid_format")])
            chosen = [item.id for item in self._settings.packages if item.id in ids]
        if isinstance(schema_id, UUID) and await self._schemas.find(owner_id, schema_id) is None:
            raise tool_errors.schema_not_found()
        selection: Selection | None = None
        if isinstance(db, SelectionInput):
            assert self._databases is not None  # флаг проверен выше
            async with self._uow_factory() as uow:
                await self._project(uow, owner_id, project_id)
            selection = await self._databases.validate_selection(user, db.connection_id, db.tables)
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id, lock=True)
            if project.web and packages:
                raise validation_error([field_error("packages", "invalid_format")])
            if catalog_files is not None and project.kind != "catalog":
                raise validation_error([field_error("catalog_files", "invalid_format")])
            if chosen is not None and chosen != project.packages:
                project.packages = chosen
                project.revision += 1
            if not isinstance(schema_id, _Unset):
                project.schema_id = schema_id
                if schema_id is not None:
                    _select(project, None)
            if not isinstance(db, _Unset):
                _select(project, selection)
                if selection is not None:
                    project.schema_id = None
            if catalog_files is not None:
                await self._set_catalog(uow, project, catalog_files)
            project.updated_at = self._clock.now()
            await uow.code.save_project(project)
            return await self._details(uow, owner_id, project)

    @staticmethod
    def _check_catalog_input(items: Sequence[CatalogInput]) -> None:
        if len({item.file_id for item in items}) != len(items):
            raise validation_error([field_error("catalog_files", "invalid_format")])
        for item in items:
            if not item.clients:
                raise validation_error([field_error("catalog_files", "required")])
            if len(set(item.clients)) != len(item.clients):
                raise validation_error([field_error("catalog_files", "invalid_format")])

    async def _set_catalog(
        self, uow: CodeUnitOfWork, project: Project, items: Sequence[CatalogInput]
    ) -> None:
        """Настройки Каталога: без смены ревизий файла и проекта и без версий (§7.5.16)."""
        files = {file.id: file for file in await uow.code.files(project.owner_id, project.id)}
        if any(item.file_id not in files for item in items):
            raise validation_error([field_error("catalog_files", "unknown_value")])
        for item in items:
            file = files[item.file_id]
            file.catalog_insertion = item.insertion
            file.catalog_clients = [client for client in CLIENTS if client in item.clients]
            await uow.code.save_file(project.owner_id, file)

    async def delete(self, owner_id: UUID, project_id: UUID) -> None:
        """Удалить проект каскадом: файлы, версии, сборку, диалог помощника (§7.5.3)."""
        async with self._uow_factory() as uow:
            if not await uow.code.delete_project(owner_id, project_id):
                raise not_found()

    # --- файлы ---

    def _check_content(self, content: str) -> None:
        if (code := content_error(content, self._settings.file_max_bytes)) is not None:
            raise validation_error([field_error("content", code)])

    def _check_limits(self, files: Sequence[ProjectFile]) -> None:
        limit = limit_exceeded(len(files), sum(file.size_bytes for file in files), self.limits)
        if limit is not None:
            raise errors.project_limit(limit, self._limit_value(limit))

    async def _touch(self, uow: CodeUnitOfWork, project: Project) -> None:
        project.revision += 1
        project.updated_at = self._clock.now()
        await uow.code.save_project(project)

    async def add_file(
        self, owner_id: UUID, project_id: UUID, path: str, content: str
    ) -> ProjectFile:
        """Новый файл проекта (§7.5.3); у однофайлового вида — `409 single_file_project`."""
        self._check_content(content)
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id, lock=True)
            limits = self.limits_of(project)
            if limits.single_file:
                raise errors.single_file_project()
            check_path(path, limits.extensions)
            files = await uow.code.files(owner_id, project_id)
            same = next((file for file in files if file.path.lower() == path.lower()), None)
            if same is not None:
                raise errors.file_exists(same.path)
            if conflicts(path, [file.path for file in files]):
                raise validation_error([_field("path", "path_conflict")])
            file = self._new_file(project, path, content, self._clock.now())
            self._check_limits([*files, file])
            await uow.code.add_file(owner_id, file)
            await self._touch(uow, project)
        return file

    async def update_file(
        self,
        owner_id: UUID,
        project_id: UUID,
        file_id: UUID,
        revision: int,
        path: str | None,
        content: str | None,
    ) -> ProjectFile:
        """Правка или переименование файла с проверкой ревизии (§7.5.3)."""
        if content is not None:
            self._check_content(content)
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id, lock=True)
            if path is not None:
                check_path(path, self.limits_of(project).extensions)
            files = await uow.code.files(owner_id, project_id)
            file = next((item for item in files if item.id == file_id), None)
            if file is None:
                raise not_found()
            if file.revision != revision:
                raise errors.file_changed(file.revision)
            others = [item for item in files if item.id != file_id]
            if path is not None:
                same = next((item for item in others if item.path.lower() == path.lower()), None)
                if same is not None:
                    raise errors.file_exists(same.path)
                if conflicts(path, [item.path for item in others]):
                    raise validation_error([_field("path", "path_conflict")])
                file.path = path
            if content is not None:
                file.content = content
                file.size_bytes = utf8_size(content)
            self._check_limits([*others, file])
            file.revision += 1
            file.updated_at = self._clock.now()
            await uow.code.save_file(owner_id, file)
            await self._touch(uow, project)
        return file

    async def delete_file(self, owner_id: UUID, project_id: UUID, file_id: UUID) -> None:
        """Удалить файл проекта; у однофайлового вида — `409 single_file_project`."""
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id, lock=True)
            if self.limits_of(project).single_file:
                raise errors.single_file_project()
            files = await uow.code.files(owner_id, project_id)
            if all(file.id != file_id for file in files):
                raise not_found()
            await uow.code.delete_file(owner_id, project_id, file_id)
            await self._touch(uow, project)

    async def export(self, owner_id: UUID, project_id: UUID) -> tuple[str, bytes]:
        """Выгрузка проекта: для Visual Studio (§7.5.5) или веб-вида с памяткой (§7.5.16)."""
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id)
            files = await uow.code.files(owner_id, project_id)
        if project.web:
            memo = web_memo(
                getattr(self._settings.web_memos, project.kind),
                [(file.path, file.catalog) for file in files],
            )
            content = await asyncio.to_thread(
                self._archives.web_zip,
                [(file.path, file.content) for file in files],
                memo,
                project.updated_at,
            )
            return project.name, content
        sdk = await self.sdk()
        server = {name.lower() for name in sdk.libraries}  # DLL сервера CoGIS (§7.5.14)
        packages = [
            (item.id, item.version)
            for item in self._settings.packages
            if item.id in project.packages and f"{item.id.lower()}.dll" not in server
        ]
        content = await asyncio.to_thread(
            self._archives.export_zip,
            project.name,
            [(file.path, file.content) for file in files],
            sdk.references,
            packages,
            self._settings.export_memo,
            project.updated_at,
        )
        return project.name, content

    # --- версии ---

    async def snapshots(self, owner_id: UUID, project_id: UUID) -> list[Snapshot]:
        """Хранимые версии, новые сверху (§7.5.6)."""
        async with self._uow_factory() as uow:
            await self._project(uow, owner_id, project_id)
            return await uow.code.snapshots(owner_id, project_id)

    def _snapshot(
        self,
        project: Project,
        files: Sequence[ProjectFile] | Sequence[Mapping[str, Any]],
        reason: Literal["edit_accepted", "build_succeeded", "restored", "before_restore"],
        **extra: Any,
    ) -> Snapshot:
        listed: list[dict[str, Any]] = []
        for item in files:
            if isinstance(item, ProjectFile):
                entry: dict[str, Any] = {"path": item.path, "content": item.content}
                if item.catalog is not None:
                    entry["catalog"] = item.catalog
            else:
                entry = dict(item)
            listed.append(entry)
        return Snapshot(
            id=uuid4(),
            project_id=project.id,
            reason=reason,
            files=listed,
            project_revision=extra.get("revision", project.revision),
            accepted_files=extra.get("accepted_files"),
            restored_from=extra.get("restored_from"),
            created_at=extra.get("created_at", self._clock.now()),
        )

    async def restore(self, owner_id: UUID, project_id: UUID, snapshot_id: UUID) -> ProjectDetails:
        """Вернуть проект к версии: `before_restore`, файлы снимка, `restored` (§7.5.6)."""
        keep = self._settings.max_snapshots
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id, lock=True)
            target = await uow.code.get_snapshot(owner_id, project_id, snapshot_id)
            if target is None:
                raise not_found()
            files = await uow.code.files(owner_id, project_id)
            await uow.code.add_snapshot(
                owner_id, self._snapshot(project, files, "before_restore"), keep
            )
            now = self._clock.now()
            wanted = {item["path"].lower(): item for item in target.files}
            for file in files:
                item = wanted.pop(file.path.lower(), None)
                if item is None:
                    await uow.code.delete_file(owner_id, project_id, file.id)
                    continue
                changed = file
                if item["content"] != file.content or item["path"] != file.path:
                    changed = replace(
                        file,
                        path=item["path"],
                        content=item["content"],
                        size_bytes=utf8_size(item["content"]),
                        revision=file.revision + 1,
                        updated_at=now,
                    )
                if project.kind == "catalog":
                    changed = replace(changed)
                    _restore_catalog(changed, item)
                if changed != file:
                    await uow.code.save_file(owner_id, changed)
            for item in wanted.values():
                added = self._new_file(project, item["path"], item["content"], now)
                if project.kind == "catalog":
                    _restore_catalog(added, item)
                await uow.code.add_file(owner_id, added)
            await self._touch(uow, project)
            restored = self._snapshot(
                project, target.files, "restored", restored_from=target.created_at
            )
            await uow.code.add_snapshot(owner_id, restored, keep)
            return await self._details(uow, owner_id, project)

    # --- сборка и архив ---

    async def build(self, owner_id: UUID, project_id: UUID) -> BuildDetails:
        """Сборка кнопкой «Собрать»; закрытие соединения её не прерывает (§7.5.7)."""
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id)
        if project.web:
            raise errors.wrong_project_kind()
        if self._sandbox is None:
            raise errors.build_unavailable()
        task = asyncio.create_task(self._build(self._sandbox, owner_id, project_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return await asyncio.shield(task)

    async def compile(
        self,
        sandbox: CodeSandbox,
        project: Project,
        files: Sequence[ProjectFile] | Sequence[CompileFile],
        *,
        button: bool,
    ) -> CompileResult:
        """Одна сборка через песочницу: очередь проекта, место, запрос (§7.5.7, шаги 2–5)."""
        sources = [
            item if isinstance(item, CompileFile) else CompileFile(item.path, item.content)
            for item in files
        ]
        async with sandbox.slot(project.id, button=button) as slot:
            return await slot.compile(
                project.name, sources, with_dependencies(project.packages, self._settings)
            )

    async def _build(self, sandbox: CodeSandbox, owner_id: UUID, project_id: UUID) -> BuildDetails:
        started = time.monotonic()
        try:
            async with sandbox.slot(project_id, button=True) as slot:
                async with self._uow_factory() as uow:
                    project = await self._project(uow, owner_id, project_id)
                    files = await uow.code.files(owner_id, project_id)
                taken_at = self._clock.now()
                result = await slot.compile(
                    project.name,
                    [CompileFile(file.path, file.content) for file in files],
                    with_dependencies(project.packages, self._settings),
                )
                stored, archive = self._stored(project, result)
                async with self._uow_factory() as uow:
                    current = await self._project(uow, owner_id, project_id, lock=True)
                    await uow.code.save_build(owner_id, stored, archive)
                    if result.status == "succeeded":
                        snapshot = self._snapshot(
                            project, files, "build_succeeded", created_at=taken_at
                        )
                        await uow.code.add_snapshot(
                            owner_id, snapshot, self._settings.max_snapshots
                        )
        except BuildInProgressError as error:
            raise errors.build_in_progress() from error
        except BuildUnavailableError as error:
            raise errors.build_unavailable() from error
        logger.info(
            "code build",
            extra={
                "project_id": str(project_id),
                "status": result.status,
                "files": len(files),
                "bytes": sum(file.size_bytes for file in files),
                "errors": len(result.errors),
                "warnings": result.warnings,
                "findings": len(result.findings),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return BuildDetails(stored, current.revision != stored.project_revision)

    def _stored(self, project: Project, result: CompileResult) -> tuple[StoredBuild, bytes | None]:
        """Итог для `code_builds`; архив — при `succeeded` без находок (§7.5.7, шаг 6)."""
        archive = None
        if result.status == "succeeded" and result.dll is not None and not result.findings:
            rules = result.rules
            archive = self._archives.cogis_archive(
                project.name,
                result.dll,
                result.bundle,
                result.on_server,
                None if rules is None else rules.factory,
                []
                if rules is None
                else [(tool.title or tool.name, tool.category) for tool in rules.tools],
                result.sdk_version,
                self._settings.archive_memo,
                self._clock.now(),
            )
        selected = [
            {"id": item.id, "version": item.version}
            for item in self._settings.packages
            if item.id in project.packages
        ]
        stored = StoredBuild(
            project_id=project.id,
            status=result.status,
            project_revision=project.revision,
            sdk_version=result.sdk_version,
            packages=selected,
            diagnostics=[item.data() for item in _ordered(result)],
            diagnostics_truncated=result.diagnostics_truncated,
            rules=None if result.rules is None else result.rules.data(),
            archive_files=None if archive is None else archive.files,
            created_at=self._clock.now(),
        )
        return stored, None if archive is None else archive.content

    async def archive(self, owner_id: UUID, project_id: UUID) -> tuple[str, bytes]:
        """Архив для CoGIS: только успешная сборка без находок и не устаревшая (§7.5.7)."""
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id)
            if project.web:
                raise errors.wrong_project_kind()
            build = await uow.code.build(owner_id, project_id)
            if build is None:
                raise errors.archive_unavailable("no_build")
            if build.status != "succeeded":
                raise errors.archive_unavailable("not_built")
            if build.findings:
                raise errors.archive_unavailable("findings")
            if build.project_revision != project.revision:
                raise errors.archive_unavailable("outdated")
            content = await uow.code.archive(owner_id, project_id)
        if content is None:
            raise errors.archive_unavailable("not_built")
        return project.name, content

    # --- правки помощника ---

    async def edit(self, owner_id: UUID, project_id: UUID, message_id: UUID) -> EditDetails:
        """Правка с предлагаемым и текущим содержимым каждого файла (§7.5.1)."""
        async with self._uow_factory() as uow:
            await self._project(uow, owner_id, project_id)
            stored = await uow.code.edit(owner_id, project_id, message_id)
            if stored is None:
                raise not_found()
            files = await uow.code.files(owner_id, project_id)
        by_path = {file.path.lower(): file for file in files}
        listed = []
        for item in stored.summary["files"]:
            base, after = stored.files[item["path"]]
            current = by_path.get(item["path"].lower())
            outdated = (
                current is not None
                if item["change"] == "added"
                else current is None or current.revision != base
            )
            listed.append(
                {
                    **item,
                    "after": after,
                    "current": None
                    if current is None
                    else {"revision": current.revision, "content": current.content},
                    "outdated": outdated,
                }
            )
        return EditDetails(stored.summary, listed)

    async def decide(
        self,
        owner_id: UUID,
        project_id: UUID,
        message_id: UUID,
        accept: Sequence[tuple[str, int | None]],
    ) -> tuple[dict[str, Any], ProjectDetails]:
        """Решение по правке: всё, часть файлов или ничего, один раз (§7.5.10)."""
        async with self._uow_factory() as uow:
            project = await self._project(uow, owner_id, project_id, lock=True)
            stored = await uow.code.edit(owner_id, project_id, message_id)
            if stored is None:
                raise not_found()
            summary = stored.summary
            if summary["status"] != "proposed":
                raise errors.edit_decided()
            paths = [item["path"] for item in summary["files"]]
            chosen = [path for path, _ in accept]
            if any(path not in paths for path in chosen):
                raise validation_error([field_error("accept", "unknown_value")])
            if len(set(chosen)) != len(chosen):
                raise validation_error([field_error("accept", "invalid_format")])
            files = await uow.code.files(owner_id, project_id)
            by_path = {file.path.lower(): file for file in files}
            stale = []
            for path, revision in accept:
                current = by_path.get(path.lower())
                if revision is None:
                    others = [file.path for file in files]
                    if current is not None or conflicts(path, others):
                        stale.append(path)
                elif current is None or current.revision != revision:
                    stale.append(path)
            if stale:
                raise errors.edit_outdated(stale)
            now = self._clock.now()
            result = {file.path.lower(): file for file in files}
            for path, _ in accept:
                content = stored.files[path][1]
                current = result.get(path.lower())
                if current is None:
                    result[path.lower()] = self._new_file(project, path, content, now)
                else:
                    result[path.lower()] = replace(
                        current,
                        content=content,
                        size_bytes=utf8_size(content),
                        revision=current.revision + 1,
                        updated_at=now,
                    )
            self._check_limits(list(result.values()))
            for path, _ in accept:
                file = result[path.lower()]
                if path.lower() in by_path:
                    await uow.code.save_file(owner_id, file)
                else:
                    await uow.code.add_file(owner_id, file)
            decided = {
                **summary,
                "status": "accepted" if accept else "rejected",
                "files": [
                    {**item, "decision": "accepted" if item["path"] in chosen else "rejected"}
                    for item in summary["files"]
                ],
                "decided_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            await uow.code.decide_edit(owner_id, project_id, message_id, decided)
            if accept:
                await self._touch(uow, project)
                snapshot = self._snapshot(
                    project,
                    sorted(result.values(), key=lambda file: file.path.lower()),
                    "edit_accepted",
                    accepted_files=len(accept),
                )
                await uow.code.add_snapshot(owner_id, snapshot, self._settings.max_snapshots)
            return decided, await self._details(uow, owner_id, project)


def _select(project: Project, selection: Selection | None) -> None:
    """Записать выбор подключения в поля проекта; `None` — снять."""
    project.db_connection_id = None if selection is None else selection.connection_id
    project.db_connection_title = None if selection is None else selection.title
    project.db_tables = [] if selection is None else selection.tables_data()


def _restore_catalog(file: ProjectFile, item: Mapping[str, Any]) -> None:
    """Настройки Каталога из версии; версия без них — по расширению (§7.5.16)."""
    catalog = item.get("catalog")
    if catalog is None:
        file.catalog_insertion, file.catalog_clients = default_catalog(file.path)
    else:
        file.catalog_insertion = catalog["insertion"]
        file.catalog_clients = list(catalog["clients"])


def _ordered(result: CompileResult) -> list[Any]:
    """Сообщения сборки: сначала ошибки, затем предупреждения, порядок песочницы внутри."""
    return [*result.errors, *(item for item in result.diagnostics if item.severity == "warning")]
