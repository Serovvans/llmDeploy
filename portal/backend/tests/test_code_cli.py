"""Команды `portal code-packages` и `portal eval-code` (docs/portal-api.md §13.4)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from portal import cli
from portal.code.sandbox import HttpCodeSandbox
from portal.core import settings as settings_module
from portal.core.container import CodeEvalContainer, build_code_eval_container
from portal.core.settings import Settings
from portal.kb.ports import Retrieval
from portal.llm.ports import ContentDelta, Finished, TextPart
from tests.code_support import FakeSandboxServer, code_settings, edit_answer, failed, succeeded
from tests.conftest import CONFIG_DIR
from tests.support import FakeKnowledge, ScriptedModel

_TOOL = "AddContactTool.cs"


def _override(tmp_path: Path, code: dict[str, Any]) -> Path:
    path = tmp_path / "config.override.yaml"
    path.write_text(yaml.safe_dump({"code": code}), encoding="utf-8")
    return path


@pytest.fixture
def only_db_password(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Окружение разового контейнера: только `PORTAL_DB_PASSWORD` (§13.4)."""
    for name in ("PORTAL_SECRET_KEY", "PORTAL_LLM_API_KEY", "QDRANT_API_KEY", "MAX_MODEL_LEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PORTAL_DB_PASSWORD", "x")
    yield


def _use_override(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setattr(
        cli, "load_settings", lambda env: settings_module.load_settings(env, CONFIG_DIR, path)
    )


@pytest.mark.usefixtures("only_db_password")
def test_code_packages_prints_only_the_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_override(monkeypatch, tmp_path / "missing.yaml")
    assert cli.main(["code-packages"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    lines = captured.out.split("\n")
    assert lines[-1] == ""  # каждая строка — с `\n`, заголовка нет
    rows = [line.split("\t") for line in lines[:-1]]
    loaded = settings_module.load_settings({"PORTAL_DB_PASSWORD": "x"}, CONFIG_DIR, tmp_path / "m")
    assert rows == [[item.id, item.version, item.sha512] for item in loaded.code.packages]


@pytest.mark.usefixtures("only_db_password")
def test_code_packages_empty_set_and_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _use_override(monkeypatch, _override(tmp_path, {"packages": []}))
    assert cli.main(["code-packages"]) == 0
    assert capsys.readouterr().out == ""
    _use_override(monkeypatch, _override(tmp_path, {"max_files": 0}))
    assert cli.main(["code-packages"]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err


pytestmark = pytest.mark.anyio


def _tasks(directory: Path) -> Path:
    tasks = [
        {
            "id": "first",
            "group": "database",
            "project": {"example": "AddressTool"},
            "schema": "CREATE TABLE contacts (id int);",
            "action": "write",
            "request": "Добавьте проверку.",
        },
        {
            "id": "fixed",
            "group": "fix",
            "project": {"name": "Broken", "packages": [], "files": {_TOOL: "class A { x }"}},
            "schema": None,
            "action": "fix",
            "request": "Исправить ошибки сборки",
        },
    ]
    directory.mkdir()
    (directory / "tasks.yaml").write_text(yaml.safe_dump(tasks, allow_unicode=True), "utf-8")
    return directory


def test_eval_code_needs_build_flag(
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    _use_override(monkeypatch, _override(tmp_path, {"build": {"enabled": False}}))
    assert cli.main(["eval-code", "--set", str(_tasks(tmp_path / "set"))]) == 1
    assert "code.build.enabled" in capsys.readouterr().err


async def test_eval_code_runs_tasks_with_portal_functions(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = code_settings(settings)
    server = FakeSandboxServer()
    client = httpx.AsyncClient(transport=httpx.MockTransport(server.handle))
    model = ScriptedModel()
    knowledge = FakeKnowledge()
    knowledge.result = Retrieval((), "ПРАВИЛА БАЗЫ", "")
    # Задача 1: правка собирается сразу. Задача 2: исходная сборка с ошибкой, правка —
    # с ошибкой, попытка цикла — собрано.
    server.replies += [succeeded(), failed(), failed(), succeeded()]
    model.scripts += [
        (ContentDelta(edit_answer((_TOOL, "// one"))), Finished("stop")),
        (ContentDelta(edit_answer((_TOOL, "// two"))), Finished("stop")),
        (ContentDelta(edit_answer((_TOOL, "// three"))), Finished("stop")),
    ]
    built: list[CodeEvalContainer] = []

    def fake(settings_: Settings) -> CodeEvalContainer:
        container = build_code_eval_container(
            settings_, model, knowledge, HttpCodeSandbox(client, settings_.code.build)
        )
        built.append(container)
        return container

    monkeypatch.setattr(cli, "build_code_eval_container", fake)
    out = tmp_path / "result.json"
    report = await cli._eval_code(target, _tasks(tmp_path / "set"), out)
    await client.aclose()
    data = json.loads(out.read_text(encoding="utf-8"))
    rows = [
        (item["id"], item["result"], item["builds"], item["model_calls"]) for item in data["tasks"]
    ]
    assert rows == [
        ("first", "first_try", 1, 1),
        ("fixed", "after_fix", 2, 2),
    ]  # fmt: skip
    assert data["groups"]["всего"] == {"first_try": 1, "after_fix": 1, "failed": 0}
    assert "// one" not in report and "// one" not in out.read_text(encoding="utf-8")
    # Тот же запрос, что у помощника: схема, правила базы знаний, промпты из YAML.
    system, question = (message.parts[0] for message in model.requests[0].messages)
    assert isinstance(system, TextPart) and isinstance(question, TextPart)
    assert model.requests[0].messages[0].role == "system"
    assert "ПРАВИЛА БАЗЫ" in system.text
    assert target.code.prompts.system.write.strip()[:40] in system.text
    assert "CREATE TABLE contacts" in question.text
    assert len(built) == 1
