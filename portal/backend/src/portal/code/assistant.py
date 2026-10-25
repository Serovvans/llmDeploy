"""Помощник проекта: правила диалога `code`, правка и цикл исправления (§7.5.8–7.5.11).

Запрос строит одна функция `build_code_plan` (`portal/code/prompt.py`) — для ответа,
оценки памяти и контрольного набора; попытку цикла — `fix_attempt_messages`. Правка не
меняет файлы проекта: она только сохраняется и ждёт решения сотрудника.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from portal.code import errors
from portal.code.domain import (
    BuildView,
    CodeAction,
    CodeLimits,
    EditFile,
    ParsedEdit,
    Project,
    ProjectFile,
    ProjectView,
    ReferenceType,
    SchemaView,
    StoredBuild,
    TargetView,
)
from portal.code.edit import parse_edit, same_content
from portal.code.paths import utf8_size
from portal.code.ports import (
    BuildUnavailableError,
    CodeUnitOfWorkFactory,
    CompileFile,
    CompileResult,
)
from portal.code.prompt import CodePlan, build_code_plan, fix_attempt_messages, messages_tokens
from portal.code.service import CodeService
from portal.core.errors import field_error, not_found, validation_error
from portal.core.settings import Settings
from portal.dialogs import errors as dialog_errors
from portal.dialogs.context import Turn, fit_memory, head_turn, with_head_rules
from portal.dialogs.ports import AnswerPlan
from portal.kb.ports import KnowledgeBase, KnowledgeSelection, KnowledgeUnavailableError
from portal.llm.ports import (
    ChatModel,
    ChatRequest,
    ContentDelta,
    ContextOverflowError,
    Finished,
    ModelMessage,
    ModelOverloadedError,
    ModelUnavailableError,
    TokenEstimator,
)
from portal.tools.sql_schemas import SqlSchemaService

_COGIS_DOCS = KnowledgeSelection(frozenset({"shared"}), cogis_only=True)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# Сборка файлов проекта с применённой правкой; ответ модели на попытку — текст и признак
# «упёрся в предел длины», `None` — попытка не удалась (сбой модели).
type Compile = Callable[[Sequence[CompileFile]], Awaitable[CompileResult]]
type Ask = Callable[[Sequence[ModelMessage]], Awaitable[tuple[str, bool] | None]]
type Report = Callable[[Mapping[str, Any]], None]


def build_view(build: StoredBuild | None, revision: int) -> BuildView | None:
    """Последняя сборка проекта для блока `<сборка>`."""
    if build is None:
        return None
    return BuildView(
        build.status,
        build.project_revision != revision,
        tuple(build.diagnostics),
        tuple(build.findings or ()),
    )


def result_view(result: CompileResult) -> BuildView:
    """Сборка цикла исправления для попытки: сообщения этой правки, без «устарела»."""
    return BuildView(
        result.status,
        False,
        tuple(item.data() for item in result.diagnostics),
        tuple(item.data() for item in result.findings),
    )


def target_view(params: Mapping[str, Any]) -> TargetView | None:
    """Снимок выделения из сохранённых параметров вопроса (§7.5.8)."""
    raw = params.get("target")
    if not isinstance(raw, Mapping):
        return None
    return TargetView(
        str(raw["file_id"]),
        str(raw["path"]),
        int(raw["start_line"]),
        int(raw["end_line"]),
        str(raw["text"]),
    )


def sent_paths(
    files: Sequence[ProjectFile], file_ids: Sequence[str] | None, target: TargetView | None
) -> list[str] | None:
    """Пути отправляемых файлов: `None` — все; файл выделения уходит всегда (§7.5.8).

    Удалённые с тех пор файлы пропускаются (повторная генерация, §7.5.8).
    """
    if file_ids is None:
        return None
    wanted = set(file_ids)
    if target is not None:
        wanted.add(target.file_id)
    return [file.path for file in files if str(file.id) in wanted]


def apply_edit(project: ProjectView, edit: Mapping[str, EditFile]) -> ProjectView:
    """Проект с применённой правкой цикла: изменённые и новые файлы (§7.5.11).

    `edit` — по пути в нижнем регистре. У новых файлов ревизии нет (0): базовые ревизии
    правки всегда берутся по файлам проекта, а не по промежуточным попыткам.
    """
    files: list[ProjectFile] = []
    for file in project.files:
        item = edit.get(file.path.lower())
        if item is None:
            files.append(file)
        else:
            files.append(replace(file, content=item.content, size_bytes=utf8_size(item.content)))
    known = {file.path.lower() for file in project.files}
    for key, item in edit.items():
        if key not in known:
            files.append(
                ProjectFile(
                    uuid4(), uuid4(), item.path, item.content, utf8_size(item.content), 0, _EPOCH
                )
            )
    return ProjectView(project.name, files)


def verification(last: CompileResult | None, builds: int) -> dict[str, Any]:
    """Итог по последней законченной сборке цикла; без сборок — `unavailable`."""
    if last is None:
        return {
            "status": "unavailable",
            "builds": 0,
            "warnings": 0,
            "diagnostics": [],
            "findings": [],
        }
    return {
        "status": last.status,
        "builds": builds,
        "warnings": last.warnings,
        "diagnostics": [item.data() for item in last.errors],
        "findings": [item.data() for item in last.findings],
    }


@dataclass
class FixLoop:
    """Цикл исправления (§7.5.11): итоговая правка, отказы, итог и счётчики."""

    edit: dict[str, EditFile]
    rejected: list[dict[str, str]]
    last: CompileResult | None = None
    builds: int = 0
    asked: int = 0

    @property
    def verification(self) -> dict[str, Any]:
        """Поле `CodeEdit.verification`."""
        return verification(self.last, self.builds)


async def run_fix_loop(
    project: ProjectView,
    first: ParsedEdit,
    limits: CodeLimits,
    max_attempts: int,
    compile_files: Compile,
    ask: Ask,
    attempt_messages: Callable[[ProjectView, CompileResult], list[ModelMessage] | None],
    report: Report,
    loop: FixLoop | None = None,
) -> FixLoop:
    """Собрать правку, при ошибках дать модели ещё попытку — не больше `max_attempts`.

    Ход прерывается отменой задачи (срок `code.fix.timeout_seconds`, остановка): `loop`
    тогда хранит последнюю законченную сборку. `attempt_messages` — запрос попытки или
    `None`, если он не помещается в бюджет `B` (попытки нет).
    """
    state = loop or FixLoop({}, [])
    state.edit = {item.path.lower(): item for item in first.files}
    state.rejected = list(first.rejected)
    actual = {file.path.lower(): file for file in project.files}
    attempt = 0
    while True:
        report({"stage": "building", "attempt": attempt, "max_attempts": max_attempts})
        current = apply_edit(project, state.edit)
        try:
            result = await compile_files([CompileFile(f.path, f.content) for f in current.files])
        except BuildUnavailableError:
            return state
        state.builds += 1
        state.last = result
        if (not result.errors and not result.findings) or attempt >= max_attempts:
            return state
        attempt += 1
        report({"stage": "fixing", "attempt": attempt, "max_attempts": max_attempts})
        messages = attempt_messages(current, result)
        if messages is None:
            return state
        state.asked += 1
        answer = await ask(messages)
        if answer is None:
            return state
        text, cut = answer
        parsed = parse_edit(text, current, limits, "length_limit" if cut else "complete")
        state.rejected.extend(parsed.rejected)
        if not parsed.files:
            return state
        for item in parsed.files:
            key = item.path.lower()
            real = actual.get(key)
            if real is not None and same_content(real.content, item.content):
                state.edit.pop(key, None)
            elif real is not None:
                state.edit[key] = EditFile(real.path, "modified", item.content, real.revision)
            else:
                state.edit[key] = EditFile(item.path, "added", item.content, None)


def edit_summary(
    files: Sequence[EditFile],
    rejected: Sequence[Mapping[str, str]],
    incomplete: bool,
    checked: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Сводка `CodeEdit` до решения (§7.5.1)."""
    return {
        "status": "proposed" if files else "rejected",
        "files": [{"path": item.path, "change": item.change, "decision": None} for item in files],
        "rejected_files": [dict(item) for item in rejected],
        "incomplete": incomplete,
        "verification": None if checked is None else dict(checked),
        "decided_at": None,
    }


class CodeTool:
    """Вид диалога `code`: порты `DialogTool` и `AnswerEditor` (§7.5.8–7.5.11)."""

    def __init__(
        self,
        uow_factory: CodeUnitOfWorkFactory,
        projects: CodeService,
        schemas: SqlSchemaService,
        knowledge: KnowledgeBase,
        model: ChatModel,
        estimator: TokenEstimator,
        settings: Settings,
        max_model_len: int,
    ) -> None:
        """Получить зависимости явно; `max_model_len` — контекст модели (§7.5.9)."""
        self._uow_factory = uow_factory
        self._projects = projects
        self._schemas = schemas
        self._knowledge = knowledge
        self._model = model
        self._estimator = estimator
        self._code = settings.code
        self._head_rules = settings.memory.head_rules_prompt
        self._budget = (
            max_model_len - settings.code.max_output_tokens - settings.llm.safety_margin_tokens
        )
        self._reserve = settings.kb.context_max_tokens

    # --- разбор тела запроса (§7.5.8, §5.10.3) ---

    async def _project_files(self, owner_id: UUID, dialog_id: UUID) -> list[ProjectFile]:
        async with self._uow_factory() as uow:
            project = await uow.code.project_by_dialog(owner_id, dialog_id)
            if project is None:
                raise not_found()
            return await uow.code.files(owner_id, project.id)

    async def question_params(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        action: CodeAction,
        file_ids: Sequence[UUID] | None,
        target: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Проверить `file_ids` и `target` по файлам проекта; параметры вопроса для хранения."""
        files = {file.id: file for file in await self._project_files(owner_id, dialog_id)}
        if file_ids is not None:
            if len(set(file_ids)) != len(file_ids):
                raise validation_error([field_error("file_ids", "invalid_format")])
            if any(item not in files for item in file_ids):
                raise validation_error([field_error("file_ids", "unknown_value")])
        snapshot = None
        if target is not None:
            file = files.get(target["file_id"])
            if file is None:
                raise validation_error([field_error("target", "unknown_value")])
            lines = file.content.split("\n")
            start, end = target["start_line"], target["end_line"]
            if not 1 <= start <= end <= len(lines):
                raise validation_error([field_error("target", "invalid_format")])
            snapshot = {
                "file_id": str(file.id),
                "path": file.path,
                "start_line": start,
                "end_line": end,
                "text": "\n".join(lines[start - 1 : end]),
            }
        return {
            "action": action,
            "file_ids": None if file_ids is None else [str(item) for item in file_ids],
            "target": snapshot,
        }

    async def memory_params(
        self, owner_id: UUID, dialog_id: UUID, action: CodeAction, files: str | None
    ) -> dict[str, Any]:
        """Строка запроса оценки: `files` нет — все, пустая — ни одного, иначе через запятую."""
        known = {str(file.id) for file in await self._project_files(owner_id, dialog_id)}
        chosen = None
        if files is not None:
            chosen = [item.strip() for item in files.split(",")] if files else []
            if any(item not in known for item in chosen):
                raise validation_error([field_error("files", "unknown_value")])
        return {"action": action, "file_ids": chosen, "target": None}

    # --- DialogTool ---

    async def accept(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        content: str,
        params: Mapping[str, Any],
        *,
        regenerate: bool = False,
    ) -> list[str] | None:
        """`fix` — только при сборке с ошибками или находками; при повторе не проверяется."""
        if params["action"] != "fix" or regenerate:
            return None
        async with self._uow_factory() as uow:
            project = await uow.code.project_by_dialog(owner_id, dialog_id)
            if project is None:
                raise not_found()
            build = await uow.code.build(owner_id, project.id)
        if build is None or not build.has_problems:
            raise errors.nothing_to_fix()
        return None

    async def plan(
        self, owner_id: UUID, dialog_id: UUID, content: str, params: Mapping[str, Any]
    ) -> AnswerPlan:
        """Запрос помощника (§7.5.9); не помещается — `message_too_long` или `project_too_large`.

        Та же проверка, что у отправки, отвечает и оценке памяти: `level = "overflow"`
        по-прежнему значит, что следующий вопрос отбросит начало истории.
        """
        async with self._uow_factory() as uow:
            project = await uow.code.project_by_dialog(owner_id, dialog_id)
            dialog = await uow.dialogs.get_dialog(owner_id, dialog_id)
            if project is None or dialog is None:
                raise not_found()
            files = await uow.code.files(owner_id, project.id)
            build = await uow.code.build(owner_id, project.id)
        schema = None
        if project.schema_id is not None:
            found = await self._schemas.find(owner_id, project.schema_id)
            schema = None if found is None else SchemaView(found.name, found.content)
        target = target_view(params)
        sdk = await self._projects.sdk()
        try:
            documented: bool | None = await self._knowledge.has_cogis_documentation()
        except KnowledgeUnavailableError:
            documented = None  # недоступность базы покажет сам поиск (§7.2)
        plan = self.build(
            params["action"],
            content,
            ProjectView(project.name, files),
            sent_paths(files, params["file_ids"], target),
            target,
            build_view(build, project.revision),
            schema,
            sdk.types,
            documented,
        )
        self.check_fits(plan, head_turn(dialog))
        return AnswerPlan(
            system=plan.system,
            question=Turn("user", plan.question),
            effort=self._code.reasoning_effort,
            max_tokens=self._code.max_output_tokens,
            selection=_COGIS_DOCS,
        )

    def check_fits(self, plan: CodePlan, head: Turn | None) -> None:
        """Вопрос с проектом помещается при пустой истории и резерве под найденное (§7.5.9).

        Длинный сам вопрос — `message_too_long`, не помещаются файлы — `project_too_large`.
        """
        system = with_head_rules(plan.system, head, self._head_rules)
        budget = self._budget - self._reserve
        for text, failure in (
            (plan.without_project, dialog_errors.message_too_long),
            (plan.question, errors.project_too_large),
        ):
            fit = fit_memory(system, [], Turn("user", text), budget, self._estimator, head=head)
            if fit.used > fit.limit:
                raise failure()

    def build(
        self,
        action: CodeAction,
        content: str,
        project: ProjectView,
        sent: Sequence[str] | None,
        target: TargetView | None,
        build: BuildView | None,
        schema: SchemaView | None,
        reference: Sequence[ReferenceType] | None,
        documented: bool | None,
    ) -> CodePlan:
        """`build_code_plan` с настройками раздела — одна сборка запроса для всех (§7.5.9)."""
        return build_code_plan(
            action,
            content,
            project,
            sent,
            target,
            build,
            schema,
            reference,
            documented,
            self._code.prompts,
            self._estimator,
            reference_always=self._code.reference_always,
            reference_max_tokens=self._code.reference_max_tokens,
            build_messages_max=self._code.build_messages_max,
        )

    async def review(self, plan: AnswerPlan, content: str) -> Mapping[str, Any] | None:
        """Проверки блоков `sql` у этого вида нет."""
        return None

    async def test_run(
        self,
        owner_id: UUID,
        plan: AnswerPlan,
        content: str,
        report: Callable[[Mapping[str, Any]], None],
    ) -> None:
        """Проверки на тестовых данных у этого вида нет."""

    # --- AnswerEditor ---

    def attempt_messages(
        self, project: ProjectView, result: CompileResult, reference: Sequence[ReferenceType] | None
    ) -> list[ModelMessage] | None:
        """Запрос попытки цикла (§12.1); не помещается в бюджет `B` — `None`."""
        messages = fix_attempt_messages(
            project,
            result_view(result),
            reference,
            self._code.prompts,
            self._estimator,
            reference_always=self._code.reference_always,
            reference_max_tokens=self._code.reference_max_tokens,
            build_messages_max=self._code.build_messages_max,
        )
        return None if messages_tokens(messages, self._estimator) > self._budget else messages

    async def ask(self, messages: Sequence[ModelMessage]) -> tuple[str, bool] | None:
        """Попытка цикла: поток модели собирается внутри; сбой — `None` (§12.1)."""
        request = ChatRequest(messages, self._code.reasoning_effort, self._code.max_output_tokens)
        text = ""
        stream = self._model.stream(request)
        try:
            async for event in stream:
                if isinstance(event, ContentDelta):
                    text += event.text
                elif isinstance(event, Finished):
                    return text, event.reason == "length"
        except (ModelOverloadedError, ModelUnavailableError, ContextOverflowError):
            return None
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()  # отмена задачи обрывает запрос к модели
        return None

    async def propose(
        self,
        owner_id: UUID,
        dialog_id: UUID,
        answer_id: UUID,
        params: Mapping[str, Any],
        content: str,
        status: Literal["complete", "length_limit"],
        report: Callable[[Mapping[str, Any]], None],
    ) -> Mapping[str, Any] | None:
        """Разобрать правку ответа; у `fix` — цикл исправления; сохранить (§7.5.10)."""
        action = cast(CodeAction, params["action"])
        if action == "explain":
            return None
        async with self._uow_factory() as uow:
            project = await uow.code.project_by_dialog(owner_id, dialog_id)
            if project is None:
                return None
            files = await uow.code.files(owner_id, project.id)
        view = ProjectView(project.name, files)
        parsed = parse_edit(content, view, self._projects.limits, status)
        if not parsed.files and not parsed.rejected:
            return None
        chosen, rejected, checked = parsed.files, parsed.rejected, None
        if action == "fix" and parsed.files:
            loop = await self._fix(project, view, parsed, report)
            chosen, rejected, checked = list(loop.edit.values()), loop.rejected, loop.verification
        summary = edit_summary(chosen, rejected, parsed.incomplete, checked)
        stored = {item.path: (item.base_revision, item.content) for item in chosen}
        async with self._uow_factory() as uow:
            saved = await uow.code.save_edit(owner_id, dialog_id, answer_id, summary, stored)
        return summary if saved else None

    async def _fix(
        self, project: Project, view: ProjectView, parsed: ParsedEdit, report: Report
    ) -> FixLoop:
        """Цикл исправления в пределах `code.fix.timeout_seconds` от начала (§7.5.11)."""
        loop = FixLoop({}, [])
        sandbox = self._projects.sandbox
        if sandbox is None:
            loop.edit = {item.path.lower(): item for item in parsed.files}
            loop.rejected = list(parsed.rejected)
            return loop
        reference = (await self._projects.sdk()).types

        async def compile_files(files: Sequence[CompileFile]) -> CompileResult:
            return await self._projects.compile(sandbox, project, files, button=False)

        # Срок цикла кончает его с итогом последней законченной сборки (§7.5.11, шаг 3).
        with suppress(TimeoutError):
            async with asyncio.timeout(self._code.fix.timeout_seconds):
                await run_fix_loop(
                    view,
                    parsed,
                    self._projects.limits,
                    self._code.fix.attempts,
                    compile_files,
                    self.ask,
                    lambda current, result: self.attempt_messages(current, result, reference),
                    report,
                    loop,
                )
        return loop
