"""Контрольный набор раздела «Код»: прогон задач путём сотрудника (docs/portal-api.md §13.4).

Проекты живут в памяти, в базу ничего не пишется. Каждая задача идёт теми же функциями,
что диалог помощника: каркас (`wizard.py`), запрос (`CodeTool.build` → `build_code_plan`
и промпты `code.prompts.*`), поиск по документации CoGIS, разбор правки (`parse_edit`),
сборка и цикл исправления (`run_fix_loop`). Текст правок и кода в итог не попадает.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from portal.code.assistant import CodeTool, FixLoop, result_view, run_fix_loop
from portal.code.domain import CodeAction, Project, ProjectFile, ProjectView, SchemaView
from portal.code.edit import parse_edit
from portal.code.paths import utf8_size
from portal.code.ports import CompileFile, CompileResult
from portal.code.service import CodeService
from portal.code.wizard import WizardTemplates, check_form, example, scaffold
from portal.core.errors import AppError
from portal.core.settings import CodeSettings
from portal.dialogs.context import Turn
from portal.kb.ports import KnowledgeBase, KnowledgeSelection, KnowledgeUnavailableError
from portal.llm.ports import (
    ChatModel,
    ChatRequest,
    ContentDelta,
    ContextOverflowError,
    Finished,
    ModelMessage,
    TextPart,
)

Outcome = Literal["first_try", "after_fix", "failed"]
OUTCOMES: tuple[Outcome, ...] = ("first_try", "after_fix", "failed")
_ACTIONS = frozenset({"write", "fix"})
_COGIS_DOCS = KnowledgeSelection(frozenset({"shared"}), cogis_only=True)
# Поиск идёт только по общей базе: владелец личных документов не нужен (§13.4).
_NOBODY = UUID(int=0)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class EvalSetError(ValueError):
    """Набор задач не по формату §13.4; в сообщении — только номер и поле задачи."""


@dataclass(frozen=True)
class EvalTask:
    """Задача набора с готовым каркасом проекта."""

    id: str
    group: str
    name: str
    files: list[tuple[str, str]]
    packages: list[str]
    schema: str | None
    action: CodeAction
    request: str


@dataclass(frozen=True)
class TaskResult:
    """Итог задачи: только числа и коды, без текста правок и кода."""

    id: str
    group: str
    result: Outcome
    builds: int
    model_calls: int
    compiler_codes: list[str] = field(default_factory=list)
    rule_codes: list[str] = field(default_factory=list)

    def data(self) -> dict[str, Any]:
        """Строка задачи для `--out`."""
        return {
            "id": self.id,
            "group": self.group,
            "result": self.result,
            "builds": self.builds,
            "model_calls": self.model_calls,
            "compiler_codes": self.compiler_codes,
            "rule_codes": self.rule_codes,
        }


def _text(raw: Mapping[str, Any], key: str, number: int) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise EvalSetError(f"задача {number}: поле {key}")
    return value


def parse_tasks(
    raw: Any, templates: WizardTemplates, settings: CodeSettings, message_max_chars: int
) -> list[EvalTask]:
    """Задачи `tasks.yaml` → каркасы проектов теми же функциями, что у мастера и примера."""
    if not isinstance(raw, list) or not raw:
        raise EvalSetError("набор пуст или не список задач")
    order = [item.id for item in settings.packages]
    tasks: list[EvalTask] = []
    seen: set[str] = set()
    for number, item in enumerate(raw, start=1):
        if not isinstance(item, Mapping):
            raise EvalSetError(f"задача {number}: не объект")
        task_id = _text(item, "id", number)
        if task_id in seen:
            raise EvalSetError(f"задача {number}: повтор id")
        seen.add(task_id)
        action = item.get("action")
        if action not in _ACTIONS:
            raise EvalSetError(f"задача {number}: поле action")
        project = item.get("project")
        if not isinstance(project, Mapping):
            raise EvalSetError(f"задача {number}: поле project")
        request = item.get("request")
        sources = [key for key in ("wizard", "example", "files") if key in project]
        if len(sources) != 1:
            raise EvalSetError(f"задача {number}: в project ровно одно из wizard, example, files")
        try:
            if "wizard" in project:
                form = project["wizard"]
                check_form(
                    form,
                    templates,
                    settings.wizard.max_tools,
                    settings.wizard.max_parameters,
                    message_max_chars,
                )
                built = scaffold(form, templates, settings.prompts.wizard_request, order)
                name, files, packages = form["name"], built.files, built.packages
                request = request or built.assistant_request
            elif "example" in project:
                name = _text(project, "example", number)
                built = example(name, templates, order)
                files, packages = built.files, built.packages
            else:
                name = _text(project, "name", number)
                files = [(str(path), str(text)) for path, text in project["files"].items()]
                packages = [str(package) for package in project.get("packages") or []]
        except (AppError, AttributeError, KeyError, TypeError) as error:
            raise EvalSetError(f"задача {number}: каркас проекта") from error
        if not isinstance(request, str) or not request:
            raise EvalSetError(f"задача {number}: поле request")
        schema = item.get("schema")
        tasks.append(
            EvalTask(
                id=task_id,
                group=_text(item, "group", number),
                name=name,
                files=files,
                packages=packages,
                schema=schema if isinstance(schema, str) else None,
                action=cast(CodeAction, action),
                request=request,
            )
        )
    return tasks


class CodeEvaluator:
    """Прогон задачи набора; песочница обязательна — без сборки замера нет."""

    def __init__(
        self,
        tool: CodeTool,
        projects: CodeService,
        knowledge: KnowledgeBase,
        model: ChatModel,
        settings: CodeSettings,
    ) -> None:
        """Получить те же объекты, что у диалога помощника."""
        self._tool = tool
        self._projects = projects
        self._knowledge = knowledge
        self._model = model
        self._settings = settings

    async def run(self, task: EvalTask) -> TaskResult:
        """Каркас → вопрос → правка → сборка → цикл исправления (§13.4)."""
        sandbox = self._projects.sandbox
        if sandbox is None:
            raise RuntimeError("сборка выключена")
        project = _project(task)
        view = ProjectView(task.name, _files(project.id, task.files))

        async def compile_files(files: Sequence[CompileFile]) -> CompileResult:
            return await self._projects.compile(sandbox, project, files, button=False)

        build = None
        if task.action == "fix":
            build = result_view(await compile_files([CompileFile(*item) for item in task.files]))
        sdk = await self._projects.sdk()
        try:
            documented: bool | None = await self._knowledge.has_cogis_documentation()
        except KnowledgeUnavailableError:
            documented = None
        schema = None if task.schema is None else SchemaView("schema", task.schema)
        plan = self._tool.build(
            task.action, task.request, view, None, None, build, schema, sdk.types, documented
        )
        try:
            self._tool.check_fits(plan, None)
        except AppError:
            return TaskResult(task.id, task.group, "failed", 0, 0)  # не помещается в запрос
        retrieval = await self._knowledge.retrieve(task.request, _NOBODY, _COGIS_DOCS)
        system, question = plan.system, Turn("user", plan.question)
        system = f"{system}\n\n{retrieval.rules}"
        if retrieval.context:
            question = Turn("user", f"{retrieval.context}\n\n{question.text}")
        answer = await self._first_answer(system, question)
        if answer is None:
            return TaskResult(task.id, task.group, "failed", 0, 1)
        text, cut = answer
        parsed = parse_edit(
            text, view, self._projects.limits, "length_limit" if cut else "complete"
        )
        if not parsed.files:
            return TaskResult(task.id, task.group, "failed", 0, 1)
        loop = await run_fix_loop(
            view,
            parsed,
            self._projects.limits,
            self._settings.fix.attempts,
            compile_files,
            self._tool.ask,
            lambda current, result: self._tool.attempt_messages(current, result, sdk.types),
            lambda _: None,
        )
        return _result(task, loop)

    async def _first_answer(self, system: str, question: Turn) -> tuple[str, bool] | None:
        """Ответ на вопрос; перегрузка и недоступность модели прерывают весь прогон."""
        messages = [
            ModelMessage("system", [TextPart(system)]),
            ModelMessage("user", [TextPart(question.text)]),
        ]
        request = ChatRequest(
            messages, self._settings.reasoning_effort, self._settings.max_output_tokens
        )
        text = ""
        try:
            async for event in self._model.stream(request):
                if isinstance(event, ContentDelta):
                    text += event.text
                elif isinstance(event, Finished):
                    return text, event.reason == "length"
        except ContextOverflowError:
            return None
        return None


def _project(task: EvalTask) -> Project:
    return Project(uuid4(), _NOBODY, task.name, task.packages, None, 1, _EPOCH, _EPOCH)


def _files(project_id: UUID, files: Sequence[tuple[str, str]]) -> list[ProjectFile]:
    return [
        ProjectFile(uuid4(), project_id, path, content, utf8_size(content), 1, _EPOCH)
        for path, content in files
    ]


def _result(task: EvalTask, loop: FixLoop) -> TaskResult:
    last = loop.last
    clean = last is not None and last.status == "succeeded" and not last.findings
    outcome: Outcome = "failed"
    if clean:
        outcome = "first_try" if loop.builds == 1 else "after_fix"
    return TaskResult(
        task.id,
        task.group,
        outcome,
        loop.builds,
        1 + loop.asked,
        [] if last is None else [item.code for item in last.errors],
        [] if last is None else [item.rule for item in last.findings],
    )


def summary(results: Sequence[TaskResult]) -> dict[str, dict[str, int]]:
    """Число задач по группам и итогам, со строкой «всего»."""
    groups: dict[str, Counter[str]] = {}
    for item in results:
        for group in (item.group, "всего"):
            groups.setdefault(group, Counter())[item.result] += 1
    return {
        group: {outcome: counts[outcome] for outcome in OUTCOMES}
        for group, counts in sorted(groups.items(), key=lambda pair: (pair[0] == "всего", pair[0]))
    }


def format_report(results: Sequence[TaskResult]) -> str:
    """Сводка по группам с долями и строка на задачу (§13.4)."""
    lines = []
    for group, counts in summary(results).items():
        total = sum(counts.values())
        cells = "  ".join(
            f"{outcome} {counts[outcome]} ({counts[outcome] / total:.0%})" for outcome in OUTCOMES
        )
        lines.append(f"{group:<12} {total:>3}  {cells}")
    lines.append("")
    for item in results:
        codes = ",".join([*item.compiler_codes, *item.rule_codes]) or "-"
        lines.append(
            f"{item.id}\t{item.result}\tсборок {item.builds}\tобращений {item.model_calls}\t{codes}"
        )
    return "".join(f"{line}\n" for line in lines)
