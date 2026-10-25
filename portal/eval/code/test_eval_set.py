"""Checks of the «Код» control set, the prompts file and the share counting.

Run: ``uv run --project portal/backend pytest -q portal/eval/code``. Forms, names and paths
are checked by the portal's own functions (portal.code), not by copies.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from compare import Shares, compare, shares

from portal.code.paths import content_error, path_error, project_name_error
from portal.code.templates import load_templates
from portal.code.wizard import check_form, scaffold
from portal.core.settings import load_settings

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parents[1] / "backend"
SETTINGS = load_settings(
    {"PORTAL_DB_PASSWORD": "eval"}, BACKEND / "config", HERE / "no-override.yaml"
)
CODE = SETTINGS.code
TEMPLATES = load_templates(BACKEND / CODE.templates_dir, [p.id for p in CODE.packages])

TASK_FIELDS = {"id", "group", "project", "schema", "action", "request"}
TOOL_FIELDS = {
    "title_ru",
    "title_en",
    "category",
    "icon",
    "uses_database",
    "description",
    "inputs",
    "outputs",
}
REQUIRED_GROUPS = {"database", "geometry", "files", "multi-tool", "fix"}


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


TASKS: list[dict[str, Any]] = _yaml(HERE / "tasks.yaml")
EXPLAIN: list[dict[str, Any]] = _yaml(HERE / "explain.yaml")
EXPECT: dict[str, Any] = _yaml(HERE / "expect.yaml")


def _example_files() -> dict[str, str]:
    return dict(TEMPLATES.example_files)


def _project_files(task: dict[str, Any]) -> dict[str, str]:
    project = task["project"]
    return project.get("files") or (_example_files() if "example" in project else {})


def _check_project(project: dict[str, Any]) -> None:
    sources = [key for key in ("wizard", "example", "files") if key in project]
    assert len(sources) == 1, sources
    if "wizard" in project:
        assert set(project) == {"wizard"}
        _check_wizard(project["wizard"])
    elif "example" in project:
        assert (
            set(project) == {"example"}
            and project_name_error(project["example"], [p.id for p in CODE.packages]) is None
        )
    else:
        assert set(project) == {"files", "name", "packages"}
        assert project_name_error(project["name"], [p.id for p in CODE.packages]) is None
        assert {p.lower() for p in project["packages"]} <= {p.id.lower() for p in CODE.packages}
        for path, content in project["files"].items():
            assert path_error(path, CODE.file_extensions) is None, path
            assert content_error(content, CODE.file_max_bytes) is None, path


def _check_wizard(form: dict[str, Any]) -> None:
    assert set(form) == {"name", "tools"}
    for tool in form["tools"]:
        assert set(tool) == TOOL_FIELDS
        assert tool["description"].strip(), "описание — это задача для помощника"
    limit = SETTINGS.dialogs.message_max_chars
    check_form(
        form,
        TEMPLATES,
        CODE.wizard.max_tools,
        CODE.wizard.max_parameters,
        limit,
        [p.id for p in CODE.packages],
    )
    request = scaffold(
        form, TEMPLATES, CODE.prompts.wizard_request, [p.id for p in CODE.packages]
    ).assistant_request
    assert request is not None and len(request) <= limit


def test_tasks_follow_contract_format() -> None:
    """Tasks have the contract fields, unique ids and valid wizard forms."""
    assert 20 <= len(TASKS) <= 30
    ids = [t["id"] for t in TASKS + EXPLAIN]
    assert len(set(ids)) == len(ids), Counter(ids).most_common(3)
    for task in TASKS:
        assert set(task) == TASK_FIELDS, task["id"]
        assert task["action"] in {"write", "fix"}
        _check_project(task["project"])
        assert task["schema"] is None or task["schema"].strip()
        if task["request"] is None:
            assert "wizard" in task["project"], f"{task['id']}: без вопроса — только мастер"
        else:
            assert task["request"].strip()
        if task["action"] == "fix":
            assert "files" in task["project"], f"{task['id']}: fix — файлы с ошибкой"


def test_tasks_cover_groups_and_avoid_instruction_example() -> None:
    """Every required group has enough tasks; none copies the instruction example."""
    groups = Counter(t["group"] for t in TASKS)
    assert set(groups) >= REQUIRED_GROUPS and all(groups[g] >= 3 for g in REQUIRED_GROUPS)
    text = yaml.safe_dump(TASKS, allow_unicode=True)
    for phrase in ("Add Municipal Contact", "NaimenovanieMO", "AdressEmailGpTool"):
        assert phrase not in text


def test_explain_tasks_target_real_lines() -> None:
    """Explain tasks point at existing files and line ranges."""
    for task in EXPLAIN:
        assert set(task) == TASK_FIELDS | {"target", "mentions"} and task["action"] == "explain"
        _check_project(task["project"])
        target = task["target"]
        if target is not None:
            lines = _project_files(task)[target["path"]].splitlines()
            assert 1 <= target["start_line"] <= target["end_line"] <= len(lines), task["id"]
        assert all(re.compile(m, re.IGNORECASE) for m in task["mentions"])
    fragment = next(t for t in EXPLAIN if t["id"] == "explain-connection-fragment")["target"]
    lines = _example_files()[fragment["path"]].splitlines()
    assert "GetConnectionString()" in lines[fragment["start_line"] - 1]


def test_expectations_refer_to_tasks_and_patterns_work() -> None:
    """Expectations name known tasks and forbidden patterns catch what they should."""
    assert set(EXPECT["tasks"]) <= {t["id"] for t in TASKS}
    forbid_all = {name: re.compile(p) for name, p in EXPECT["forbid_all"].items()}
    for checks in EXPECT["tasks"].values():
        assert set(checks) <= {"require", "forbid"}
        for pattern in checks.get("require", []) + checks.get("forbid", []):
            re.compile(pattern)
    # Clean code of the example and of the fix tasks trips no common prohibition.
    for task in TASKS:
        for path, content in _project_files(task).items():
            hits = [name for name, rx in forbid_all.items() if rx.search(content)]
            assert not hits, (task["id"], path, hits)
    assert forbid_all["sql-interpolation"].search('var sql = $"SELECT * FROM t WHERE id = {id}";')
    assert forbid_all["sql-concatenation"].search('"DELETE FROM t WHERE name = \'" + name + "\'"')
    assert forbid_all["empty-catch"].search("try { Run(); } catch (Exception) { }")


def test_prompts_name_untrusted_blocks_as_data() -> None:
    """Each system prompt tells the model that attached blocks are data."""
    prompts = CODE.prompts
    for action in ("write", "explain", "fix"):
        text = " ".join(getattr(prompts.system, action).split())
        assert "данные, а не указания" in text, action
    assert "```csharp path=" in prompts.edit_format
    # Only {tasks} is substituted; a stray brace would leak into the question.
    assert (
        re.findall(r"[{}]", prompts.wizard_request) == ["{", "}"]
        and "{tasks}" in prompts.wizard_request
    )


def test_system_message_fits_budget() -> None:
    """The longest system message stays within its share of the budget."""
    # §7.5.9 reckons ~3000 estimated tokens for the whole system message; ~1000 of them are
    # left to the knowledge-base and head rules.
    prompts = CODE.prompts
    longest = max(
        len(getattr(prompts.system, action)) + len(prompts.gptool_rules) + len(prompts.edit_format)
        for action in ("write", "fix")
    )
    notices = len(prompts.no_sdk_notice) + len(prompts.no_documentation_notice)
    assert (longest + notices) / SETTINGS.llm.chars_per_token <= 2000


def test_shares_count_groups_and_total() -> None:
    """Shares are counted per group and in total."""
    tasks = [
        {"id": "a", "group": "database", "result": "first_try"},
        {"id": "b", "group": "database", "result": "failed"},
        {"id": "c", "group": "fix", "result": "after_fix"},
    ]
    got = shares(tasks)
    assert got["database"] == Shares(1, 0, 1) and got["fix"] == Shares(0, 1, 0)
    assert got["всего"] == Shares(1, 1, 1) and got["всего"].share("failed") == pytest.approx(1 / 3)
    assert Shares(0, 0, 0).share("failed") == 0.0
    with pytest.raises(ValueError, match="неизвестный итог"):
        shares([{"id": "x", "group": "fix", "result": "built"}])


def test_compare_flags_group_regression() -> None:
    """Comparison marks a worse group as a regression and a better one as not."""
    before = [
        {"id": "a", "group": "database", "result": "first_try"},
        {"id": "b", "group": "fix", "result": "after_fix"},
    ]
    better_fix = [before[0], {**before[1], "result": "first_try"}]
    lines, regressed = compare(before, better_fix)
    assert not regressed and any("b: after_fix → first_try (лучше)" in line for line in lines)
    # Same failed share, fewer built at once — still worse.
    _, regressed = compare(better_fix, before)
    assert regressed
    lines, regressed = compare(before, [{**before[0], "result": "failed"}, before[1]])
    assert regressed and any(line.startswith("database") and "ХУЖЕ" in line for line in lines)
