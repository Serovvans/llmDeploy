"""Тела запросов и ответов раздела «Код» (docs/portal-api.md §7.5.1, §7.5.2)."""

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field

from portal.code.domain import ProjectFile, Snapshot, StoredBuild
from portal.code.ports import ProjectPage
from portal.code.service import ProjectDetails
from portal.core.schemas import ApiTime, RequestModel
from portal.dialogs.schemas import CarriedRequest


class WizardProjectRequest(RequestModel):
    """`POST /api/code/projects` из мастера; форму проверяет сценарий (§7.5.4)."""

    source: Literal["wizard"]
    wizard: dict[str, Any]
    carried: CarriedRequest | None = None


class ExampleProjectRequest(RequestModel):
    """`POST /api/code/projects` — проект-пример."""

    source: Literal["example"]
    name: str


# Тело создания проекта: мастер или пример, по полю `source`.
CreateProjectRequest = Annotated[
    WizardProjectRequest | ExampleProjectRequest, Field(discriminator="source")
]


class PatchProjectRequest(RequestModel):
    """`PATCH /api/code/projects/{id}`: хотя бы одно поле; `schema_id: null` — без схемы."""

    packages: list[str] | None = None
    schema_id: UUID | None = None


class AddFileRequest(RequestModel):
    """`POST /api/code/projects/{id}/files`."""

    path: str
    content: str


class UpdateFileRequest(RequestModel):
    """`PATCH …/files/{file_id}`: ревизия и хотя бы одно из `path`, `content`."""

    revision: int
    path: str | None = None
    content: str | None = None


class AcceptedFile(RequestModel):
    """Принимаемый файл правки: путь из правки и ревизия, которую видел сотрудник."""

    path: str
    revision: int | None


class DecisionRequest(RequestModel):
    """`POST …/edits/{message_id}/decision` (§7.5.10); пустой список — отклонить всё."""

    accept: list[AcceptedFile]


class CodeFileOut(BaseModel):
    """`CodeFile`."""

    id: UUID
    path: str
    content: str
    revision: int
    updated_at: ApiTime

    @classmethod
    def of(cls, file: ProjectFile) -> Self:
        """Файл проекта для ответа."""
        return cls(
            id=file.id,
            path=file.path,
            content=file.content,
            revision=file.revision,
            updated_at=file.updated_at,
        )


class CodeBuildOut(BaseModel):
    """`CodeBuild` — последняя сборка кнопкой."""

    status: str
    outdated: bool
    sdk_version: str | None
    packages: list[dict[str, str]]
    diagnostics: list[dict[str, Any]]
    diagnostics_truncated: bool
    factory: str | None
    tools: list[dict[str, Any]] | None
    findings: list[dict[str, Any]] | None
    archive: dict[str, Any] | None
    created_at: ApiTime

    @classmethod
    def of(cls, build: StoredBuild, outdated: bool) -> Self:
        """Итог сборки; правила — только у успешной."""
        rules = build.rules
        return cls(
            status=build.status,
            outdated=outdated,
            sdk_version=build.sdk_version,
            packages=build.packages,
            diagnostics=build.diagnostics,
            diagnostics_truncated=build.diagnostics_truncated,
            factory=None if rules is None else rules.get("factory"),
            tools=None if rules is None else rules.get("tools"),
            findings=build.findings,
            archive=build.archive_files,
            created_at=build.created_at,
        )


class CodeProjectOut(BaseModel):
    """`CodeProject`."""

    id: UUID
    name: str
    packages: list[str]
    schema_id: UUID | None
    dialog_id: UUID
    files: list[CodeFileOut]
    build: CodeBuildOut | None
    build_running: bool
    created_at: ApiTime
    updated_at: ApiTime

    @classmethod
    def of(cls, details: ProjectDetails) -> Self:
        """Проект целиком."""
        project = details.project
        return cls(
            id=project.id,
            name=project.name,
            packages=project.packages,
            schema_id=project.schema_id,
            dialog_id=details.dialog_id,
            files=[CodeFileOut.of(file) for file in details.files],
            build=None
            if details.build is None
            else CodeBuildOut.of(details.build, details.outdated),
            build_running=details.running,
            created_at=project.created_at,
            updated_at=project.updated_at,
        )


class CreatedProjectOut(BaseModel):
    """Ответ создания и импорта проекта."""

    project: CodeProjectOut
    assistant_request: str | None
    import_: dict[str, Any] | None = Field(serialization_alias="import")


class BuildBadgeOut(BaseModel):
    """Итог последней сборки для значка в списке."""

    status: str
    outdated: bool
    findings: int | None


class CodeProjectItemOut(BaseModel):
    """`CodeProjectItem`."""

    id: UUID
    name: str
    updated_at: ApiTime
    build: BuildBadgeOut | None

    @classmethod
    def of(cls, page: ProjectPage) -> Self:
        """Строка списка проектов."""
        project, build = page.project, page.build
        badge = None
        if build is not None:
            findings = build.findings
            badge = BuildBadgeOut(
                status=build.status,
                outdated=build.project_revision != project.revision,
                findings=None if findings is None else len(findings),
            )
        return cls(id=project.id, name=project.name, updated_at=project.updated_at, build=badge)


class CodeProjectListOut(BaseModel):
    """`GET /api/code/projects`."""

    items: list[CodeProjectItemOut]
    next_cursor: str | None


class CodeSnapshotOut(BaseModel):
    """`CodeSnapshot`."""

    id: UUID
    reason: str
    created_at: ApiTime
    accepted_files: int | None
    restored_from: ApiTime | None

    @classmethod
    def of(cls, snapshot: Snapshot) -> Self:
        """Версия проекта без файлов."""
        return cls(
            id=snapshot.id,
            reason=snapshot.reason,
            created_at=snapshot.created_at,
            accepted_files=snapshot.accepted_files,
            restored_from=snapshot.restored_from,
        )


class CodeSnapshotListOut(BaseModel):
    """`GET …/snapshots`."""

    items: list[CodeSnapshotOut]


class DecisionOut(BaseModel):
    """Ответ решения по правке: правка и проект после неё."""

    edit: dict[str, Any]
    project: CodeProjectOut
