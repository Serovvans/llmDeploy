"""Проверка SQL на тестовых данных: сценарий и поток ответа (docs/portal-api.md §5.5, §7.4).

Песочница подменена — порт `SqlSandbox`; настоящая СУБД — в `test_sql_sandbox.py`.
"""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from portal.core.settings import Settings
from portal.llm.ports import ContentDelta, Finished, ModelUnavailableError, TextPart
from portal.tools.ports import (
    SandboxQueryError,
    SandboxRun,
    SandboxSetupError,
    SandboxTable,
    SandboxUnavailableError,
)
from portal.tools.sql_run import SqlTestRunner
from portal.tools.sql_syntax import SqlSyntaxChecker, parse_statements
from tests.conftest import close_portal, make_portal
from tests.support import (
    Portal,
    ScriptedModel,
    live_server,
    new_tool_dialog,
    parse_events,
    post_events,
)

pytestmark = pytest.mark.anyio

DDL = "CREATE TABLE parcels (id int, area numeric);"
DATA = json.dumps(
    {
        "statements": [
            "CREATE TABLE parcels (id int, area numeric)",
            "INSERT INTO parcels VALUES (1, 5)",
        ]
    }
)
RUN = SandboxRun(
    fixture=[SandboxTable("parcels", ["id", "area"], [["1", "5"]], False)],
    result=SandboxTable("", ["id"], [["1"]], False),
    affected_rows=1,
)
type Step = SandboxRun | Exception | asyncio.Event


class FakeSandbox:
    """Подменная песочница: исходы попыток по сценарию теста, вызовы запоминаются."""

    def __init__(self) -> None:
        self.script: list[Step] = []
        self.runs: list[tuple[list[str], str]] = []
        self.users: list[UUID] = []
        self.unavailable = False
        self.started = asyncio.Event()

    @asynccontextmanager
    async def slot(self, user_id: UUID) -> AsyncIterator["FakeSandbox"]:
        if self.unavailable:
            raise SandboxUnavailableError
        self.users.append(user_id)
        yield self

    async def run(self, setup: Sequence[str], query: str) -> SandboxRun:
        self.runs.append((list(setup), query))
        self.started.set()
        step = self.script.pop(0) if self.script else RUN
        if isinstance(step, asyncio.Event):
            await step.wait()
            return RUN
        if isinstance(step, Exception):
            raise step
        return step


def _sql(*queries: str) -> str:
    return "\n".join(f"Запрос:\n```sql\n{query}\n```" for query in queries)


def _enabled(settings: Settings, **sandbox: Any) -> Settings:
    update = {"enabled": True, **sandbox}
    sql = settings.sql.model_copy(
        update={"sandbox": settings.sql.sandbox.model_copy(update=update)}
    )
    return settings.model_copy(update={"sql": sql})


# --- сценарий ---


async def _check(
    settings: Settings, sandbox: FakeSandbox, model: ScriptedModel, content: str, **update: Any
) -> list[Mapping[str, Any]]:
    """Прогнать сценарий по тексту ответа; вернуть все сообщённые состояния."""
    checker = SqlSyntaxChecker(settings.sql.check_max_chars, settings.sql.check_timeout_seconds)
    config = _enabled(settings, **update).sql.sandbox
    reports: list[Mapping[str, Any]] = []
    try:
        runner = SqlTestRunner(sandbox, model, checker, config)
        await runner.run(uuid4(), DDL, "postgres", content, reports.append)
    finally:
        checker.close()
    return reports


def _final(reports: list[Mapping[str, Any]]) -> list[tuple[str, str | None]]:
    return [(block["status"], block["reason"]) for block in reports[-1]["blocks"]]


async def test_only_single_select_or_dml_statement_is_executed(settings: Settings) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    content = _sql(
        "DROP TABLE parcels",
        "TRUNCATE parcels",
        "CREATE TABLE x (id int)",
        "SELECT 1; DELETE FROM parcels",
        "SET statement_timeout = 0",
        "DO $$ BEGIN DELETE FROM parcels; END $$",
        "COPY parcels TO PROGRAM 'id'",
        "COMMIT",
        "EXPLAIN ANALYZE DELETE FROM parcels",
        "SELECT * INTO fresh FROM parcels",
        "SELECT 1 AS a INTO fresh UNION SELECT 2",
        "SELECT FROM WHERE",
        "DELETE FROM parcels WHERE id = 1",
        "SELECT 1",
    )
    reports = await _check(settings, sandbox, model, content, max_blocks=13)
    assert _final(reports) == [
        *[("not_checked", "statement_not_supported")] * 3,
        ("not_checked", "multiple_statements"),
        *[("not_checked", "statement_not_supported")] * 7,
        ("not_checked", "unparsed"),
        ("passed", None),
        ("not_checked", "block_limit"),
    ]
    # До песочницы дошёл только один разрешённый оператор.
    assert [query for _, query in sandbox.runs] == ["DELETE FROM parcels WHERE id = 1"]
    passed = reports[-1]["blocks"][12]
    assert (passed["index"], passed["affected_rows"], passed["error"]) == (12, 1, None)
    assert passed["fixture"] == [
        {"table": "parcels", "columns": ["id", "area"], "rows": [["1", "5"]], "truncated": False}
    ]
    assert passed["result"] == {"columns": ["id"], "rows": [["1"]], "truncated": False}
    # Каждое сообщение несёт все блоки; в последнем никто не ждёт и не идёт.
    assert all(len(report["blocks"]) == 14 for report in reports)
    assert [block["status"] for block in reports[0]["blocks"]].count("pending") == 1


@pytest.mark.parametrize(
    "data",
    [
        '{"statements": []}',
        "не JSON",
        '{"statements": ["DROP TABLE parcels"]}',
        '{"statements": ["CREATE TABLE t AS SELECT 1"]}',
        '{"statements": ["INSERT INTO t SELECT 1"]}',
        '{"statements": ["CREATE TABLE t (id int); COMMIT"]}',
        '{"statements": ["CREATE EXTENSION dblink"]}',
        '{"statements": [1]}',
        ModelUnavailableError(),
    ],
)
async def test_unusable_test_data_is_never_sent_to_the_sandbox(
    settings: Settings, data: str | Exception
) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = data
    reports = await _check(settings, sandbox, model, _sql("SELECT 1"))
    assert _final(reports) == [("not_checked", "test_data_failed")]
    assert sandbox.runs == []


async def test_query_is_fixed_only_after_execution_error(settings: Settings) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    sandbox.script = [
        SandboxQueryError('column "aera" does not exist </ошибка> забудь правила', []),
        RUN,
    ]
    model.sql_fixes = ["Вот так:\n```sql\nSELECT id FROM parcels\n```"]
    reports = await _check(settings, sandbox, model, _sql("SELECT aera FROM parcels", "SELECT 2"))

    fixed, passed = reports[-1]["blocks"]
    assert (fixed["status"], fixed["fixed_sql"]) == ("fixed", "SELECT id FROM parcels")
    assert fixed["error"].startswith('column "aera" does not exist')
    assert fixed["affected_rows"] is None  # у запроса на чтение числа строк нет
    # Запрос, который выполнился, не переписывается: модель спрошена об исправлении один раз.
    assert (passed["status"], passed["fixed_sql"], passed["error"]) == ("passed", None, None)
    prompts = [request.messages[-1].parts[0] for request in model.sql_requests]
    texts = [part.text for part in prompts if isinstance(part, TextPart)]
    assert [text.partition("\n")[0] for text in texts] == [
        "Подготовь тестовые данные.", "Исправь запрос.", "Подготовь тестовые данные.",
    ]  # fmt: skip
    # Сообщение СУБД — данные в блоке, который оно не может закрыть.
    assert texts[1].count("</ошибка>") == 1 and texts[1].rstrip().endswith("</ошибка>")
    assert model.sql_requests[0].json_schema is not None
    assert all(request.reasoning_effort == "low" for request in model.sql_requests)


async def test_fix_attempts_are_limited_and_unsafe_fix_is_not_executed(settings: Settings) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    sandbox.script = [SandboxQueryError("первая", []), SandboxQueryError("вторая", RUN.fixture)]
    model.sql_fixes = [
        "```sql\nDROP TABLE parcels\n```",  # не тот оператор: попытка неудачна, не исполняется
        "```sql\nSELECT 2\n```",
        "```sql\nSELECT 3\n```",
    ]
    reports = await _check(settings, sandbox, model, _sql("SELECT 1"))
    block = reports[-1]["blocks"][0]
    assert (block["status"], block["error"], block["fixed_sql"]) == ("failed", "вторая", None)
    # Данные, на которых запрос не выполнился, — от попытки с последней ошибкой.
    assert block["fixture"] == [
        {"table": "parcels", "columns": ["id", "area"], "rows": [["1", "5"]], "truncated": False}
    ]
    assert block["result"] is None
    assert [query for _, query in sandbox.runs] == ["SELECT 1", "SELECT 2"]

    rejected = FakeSandbox()
    rejected.script = [SandboxSetupError()]
    reports = await _check(settings, rejected, model, _sql("SELECT 1"))
    assert _final(reports) == [("not_checked", "test_data_failed")]


SCHEMA_DATA = json.dumps(
    {
        "statements": [
            "CREATE TABLE cadastre.parcels (id int, o int REFERENCES cadastre.owners (id))",
            "INSERT INTO parcels VALUES (1, 5)",
        ]
    }
)


async def test_schema_names_are_cut_only_on_the_way_to_the_sandbox(settings: Settings) -> None:
    """[А-62] В песочнице схема одна; модель и пользователь видят тексты как написаны."""
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = SCHEMA_DATA
    query = "SELECT  cadastre.parcels.id -- cadastre.x\nFROM cadastre.parcels WHERE 'a.b' <> ''"
    reports = await _check(settings, sandbox, model, _sql(query))

    # Верный запрос со схемой проходит и не помечается исправленным.
    block = reports[-1]["blocks"][0]
    assert (block["status"], block["fixed_sql"], block["error"]) == ("passed", None, None)
    # До порта доходит тот же текст без имён схем: пробелы, комментарий и строка целы.
    assert sandbox.runs == [
        (
            [
                "CREATE TABLE parcels (id int, o int REFERENCES owners (id))",
                "INSERT INTO parcels VALUES (1, 5)",
            ],
            "SELECT  parcels.id -- cadastre.x\nFROM parcels WHERE 'a.b' <> ''",
        )
    ]

    # Исправление: модель получает запрос и подготовку со схемами, `fixed_sql` — тоже со схемой.
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = SCHEMA_DATA
    sandbox.script = [SandboxQueryError('column "aera" does not exist', RUN.fixture), RUN]
    model.sql_fixes = ["```sql\nSELECT id FROM cadastre.parcels\n```"]
    reports = await _check(settings, sandbox, model, _sql("SELECT aera FROM cadastre.parcels"))
    block = reports[-1]["blocks"][0]
    assert (block["status"], block["fixed_sql"]) == ("fixed", "SELECT id FROM cadastre.parcels")
    assert [run[1] for run in sandbox.runs] == [
        "SELECT aera FROM parcels",
        "SELECT id FROM parcels",
    ]
    fix_request = model.sql_requests[1].messages[-1].parts[0]
    assert isinstance(fix_request, TextPart)
    assert "CREATE TABLE cadastre.parcels" in fix_request.text
    assert "SELECT aera FROM cadastre.parcels" in fix_request.text


async def test_same_table_name_in_different_schemas_is_not_checked(settings: Settings) -> None:
    """Без схем `a.parcels` и `b.parcels` — одна таблица: исполнялся бы уже другой запрос."""
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    content = _sql(
        "SELECT * FROM a.parcels JOIN b.parcels USING (id)",
        "SELECT * FROM cadastre.parcels JOIN Parcels AS p USING (id)",
        "WITH parcels AS (SELECT * FROM cadastre.parcels) SELECT * FROM parcels",
    )
    reports = await _check(settings, sandbox, model, content)
    assert _final(reports) == [("not_checked", "table_name_conflict")] * 3
    # Причина отбора: ни места, ни запроса к модели.
    assert sandbox.users == [] and model.sql_requests == []

    # Исправленный запрос с совпадением имён — неудачная попытка, он не исполняется.
    sandbox.script = [SandboxQueryError("первая", []), SandboxQueryError("вторая", [])]
    model.sql_fixes = [
        "```sql\nSELECT * FROM a.parcels, b.parcels\n```",
        "```sql\nSELECT 2\n```",
    ]
    reports = await _check(settings, sandbox, model, _sql("SELECT 1"))
    assert reports[-1]["blocks"][0]["status"] == "failed"
    assert [query for _, query in sandbox.runs] == ["SELECT 1", "SELECT 2"]


@pytest.mark.parametrize(
    ("sql", "bare", "conflict"),
    [
        ("SELECT 1 FROM parcels", "SELECT 1 FROM parcels", False),
        (
            'SELECT db.cadastre.parcels.id, "Кадастр"."Участки".* FROM db.cadastre . parcels'
            ' JOIN "Кадастр"."Участки" ON true ORDER BY cadastre.parcels.id',
            'SELECT parcels.id, "Участки".* FROM  parcels JOIN "Участки" ON true'
            " ORDER BY parcels.id",
            False,
        ),
        # Системные схемы, функции и типы со схемой не меняются и в сравнении не участвуют.
        (
            "SELECT pg_catalog.length(c.table_name), g::public.geometry"
            " FROM information_schema.columns AS c, public.generate_series(1, 3) AS g,"
            " cadastre.columns WHERE information_schema.columns.table_name <> ''",
            "SELECT pg_catalog.length(c.table_name), g::public.geometry"
            " FROM information_schema.columns AS c, public.generate_series(1, 3) AS g,"
            " columns WHERE information_schema.columns.table_name <> ''",
            False,
        ),
        (
            "MERGE INTO a.t USING b.s ON a.t.id = b.s.id WHEN MATCHED THEN DELETE",
            "MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE",
            False,
        ),
        ("UPDATE a.t SET x = 1 FROM a.T AS other", "UPDATE t SET x = 1 FROM T AS other", False),
        # Имена в кавычках различаются регистром: это разные таблицы, но схемы у них разные.
        ('SELECT 1 FROM a."T", b.t', 'SELECT 1 FROM "T", t', False),
        ('SELECT 1 FROM "A".t, a.t', "SELECT 1 FROM t, t", True),
        # Ссылка на столбец со схемой — не упоминание таблицы.
        ("SELECT a.t.id FROM t", "SELECT t.id FROM t", False),
    ],
)
def test_schema_names_are_cut_from_table_names_only(sql: str, bare: str, conflict: bool) -> None:
    parsed = parse_statements(sql, "postgres")
    assert parsed is not None
    assert (parsed.bare, parsed.name_conflict) == (bare, conflict)


def test_statement_with_unlocated_schema_name_is_unparsed() -> None:
    """Между именем схемы и точкой — комментарий: вырезать по месту нельзя (§7.4)."""
    assert parse_statements("SELECT 1 FROM cadastre /* c */ . parcels", "postgres") is None
    # У текста из нескольких операторов схемы не снимаются: он не исполняется.
    several = parse_statements("SELECT 1 FROM a.t; SELECT 2", "postgres")
    assert several is not None and len(several.kinds) == 2


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM db..parcels",
        "DELETE FROM db..parcels",
        "UPDATE db..parcels SET id = 1",
        "INSERT INTO db..parcels VALUES (1)",
        "MERGE INTO db..parcels USING s ON true WHEN MATCHED THEN DELETE",
    ],
)
def test_empty_schema_name_is_unparsed(sql: str) -> None:
    """Пустая схема в дереве разбора — строка, а не узел: места у неё нет."""
    assert parse_statements(sql, "postgres") is None


async def test_empty_schema_name_does_not_stop_the_other_blocks(settings: Settings) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    content = _sql("SELECT * FROM db..parcels", "SELECT id FROM cadastre.parcels")
    reports = await _check(settings, sandbox, model, content)
    assert _final(reports) == [("not_checked", "unparsed"), ("passed", None)]
    assert [query for _, query in sandbox.runs] == ["SELECT id FROM parcels"]

    # Такой же текст от модели: подготовка данных непригодна, исправление — неудачная попытка.
    model.sql_data = '{"statements": ["CREATE TABLE db..parcels (id int)"]}'
    reports = await _check(settings, FakeSandbox(), model, _sql("SELECT 1"))
    assert _final(reports) == [("not_checked", "test_data_failed")]

    model.sql_data = DATA
    failing = FakeSandbox()
    failing.script = [SandboxQueryError("первая", []), SandboxQueryError("вторая", [])]
    model.sql_fixes = ["```sql\nSELECT * FROM db..parcels\n```", "```sql\nSELECT 2\n```"]
    reports = await _check(settings, failing, model, _sql("SELECT 1"))
    assert reports[-1]["blocks"][0]["status"] == "failed"
    assert [query for _, query in failing.runs] == ["SELECT 1", "SELECT 2"]


async def test_timeout_and_unavailable_sandbox_do_not_stop_the_rest(settings: Settings) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    sandbox.script = [asyncio.Event(), SandboxUnavailableError(), RUN]
    reports = await _check(
        settings, sandbox, model, _sql("SELECT 1", "SELECT 2", "SELECT 3"), total_timeout_seconds=1
    )
    assert _final(reports) == [("timeout", None), ("unavailable", None), ("passed", None)]

    sandbox.unavailable = True
    reports = await _check(settings, sandbox, model, _sql("SELECT 1", "SELECT 2"))
    assert _final(reports) == [("unavailable", None), ("unavailable", None)]


async def test_cancelled_check_reports_stopped_blocks(settings: Settings) -> None:
    sandbox, model = FakeSandbox(), ScriptedModel()
    model.sql_data = DATA
    sandbox.script = [RUN, asyncio.Event()]
    reports: list[Mapping[str, Any]] = []
    checker = SqlSyntaxChecker(settings.sql.check_max_chars, settings.sql.check_timeout_seconds)
    runner = SqlTestRunner(sandbox, model, checker, settings.sql.sandbox)
    content = _sql("SELECT 1", "SELECT 2", "SELECT 3", "DROP TABLE t")
    task = asyncio.create_task(runner.run(uuid4(), DDL, "postgres", content, reports.append))
    try:
        async with asyncio.timeout(5):
            while len(sandbox.runs) < 2:
                await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        checker.close()
    assert _final(reports) == [
        ("passed", None), ("stopped", None), ("stopped", None),
        ("not_checked", "block_limit"),
    ]  # fmt: skip


# --- поток ответа ---


@pytest.fixture
async def checked(settings: Settings) -> AsyncIterator[tuple[Portal, FakeSandbox]]:
    """Портал с включённой проверкой SQL и подменной песочницей."""
    sandbox = FakeSandbox()
    portal = await make_portal(_enabled(settings), sql_sandbox=sandbox)
    portal.model.sql_data = DATA
    yield portal, sandbox
    await close_portal(portal)


async def _dialog(client: httpx.AsyncClient) -> tuple[str, dict[str, Any]]:
    """Диалог `sql` и тело вопроса с сохранённой схемой и включённой проверкой."""
    schema = await client.post("/api/sql/schemas", json={"name": "Кадастр", "content": DDL})
    body = {
        "content": "Напиши запрос",
        "action": "write",
        "dialect": "postgres",
        "schema_id": schema.json()["id"],
        "test_run": True,
    }
    return await new_tool_dialog(client, "sql"), body


def _reply(portal: Portal, text: str) -> None:
    portal.model.scripts.append([ContentDelta(text), Finished("stop")])


async def _answer(client: httpx.AsyncClient, dialog_id: str) -> dict[str, Any]:
    items = (await client.get(f"/api/dialogs/{dialog_id}/messages")).json()["items"]
    answer: dict[str, Any] = items[0]
    return answer


async def test_stream_reports_run_after_saved_answer_and_keeps_it_in_history(
    checked: tuple[Portal, FakeSandbox],
) -> None:
    portal, sandbox = checked
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    _reply(portal, _sql("SELECT id FROM parcels"))
    events = await post_events(client, f"/api/dialogs/{dialog_id}/messages", body)

    names = [name for name, _ in events if name != "title"]
    assert names == ["start", "delta", "sql_check", "sql_run", "sql_run", "sql_run", "done"]
    runs = [data for name, data in events if name == "sql_run"]
    assert [run["blocks"][0]["status"] for run in runs] == ["pending", "running", "passed"]
    answer = await _answer(client, dialog_id)
    assert answer["sql_run"] == runs[-1] and answer["status"] == "complete"
    assert sandbox.users == [await portal.user_id("ivanov")]
    stored = await portal.rows("SELECT params FROM messages WHERE role = 'user'")
    assert stored[0].params["test_run"] is True

    # Повторная генерация проверяет заново; со схемой, снятой в теле, — без проверки и
    # без ошибки, сохранённые параметры вопроса прежние (§5.6).
    _reply(portal, _sql("SELECT 1"))
    again = parse_events((await client.post(f"/api/dialogs/{dialog_id}/regenerate")).text)
    assert [name for name, _ in again].count("sql_run") == 3
    _reply(portal, _sql("SELECT 1"))
    response = await client.post(f"/api/dialogs/{dialog_id}/regenerate", json={"schema_id": None})
    plain = parse_events(response.text)
    assert "sql_run" not in dict(plain) and plain[-1] == ("done", {"status": "complete"})
    assert (await _answer(client, dialog_id))["sql_run"] is None
    assert len(sandbox.runs) == 2


async def test_sandbox_failures_never_break_the_answer(
    checked: tuple[Portal, FakeSandbox], monkeypatch: pytest.MonkeyPatch
) -> None:
    portal, sandbox = checked
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    path = f"/api/dialogs/{dialog_id}/messages"

    sandbox.unavailable = True
    _reply(portal, _sql("SELECT 1"))
    events = await post_events(client, path, body)
    assert events[-1] == ("done", {"status": "complete"})
    answer = await _answer(client, dialog_id)
    assert answer["sql_run"]["blocks"][0]["status"] == "unavailable"
    assert (answer["status"], answer["content"]) == ("complete", _sql("SELECT 1"))

    # Сбой самой проверки (не песочницы): ответ цел, итога проверки нет.
    sandbox.unavailable = False

    async def broken(*args: Any) -> None:
        raise RuntimeError("сбой")

    monkeypatch.setattr(SqlTestRunner, "_select", broken)
    _reply(portal, _sql("SELECT 2"))
    events = await post_events(client, path, body)
    assert events[-1] == ("done", {"status": "complete"})
    answer = await _answer(client, dialog_id)
    assert (answer["status"], answer["sql_run"]) == ("complete", None)
    assert answer["sql_check"]["blocks"][0]["valid"] is True


async def test_unexpected_failure_on_a_block_keeps_the_results_of_the_others(
    checked: tuple[Portal, FakeSandbox],
) -> None:
    """Сбой, которого порт не обещал, — исход одного блока, а не потеря всей проверки."""
    portal, sandbox = checked
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    sandbox.script = [RUN, ValueError("year -44 is out of range"), RUN]
    _reply(portal, _sql("SELECT 1", "SELECT 2", "SELECT 3"))
    events = await post_events(client, f"/api/dialogs/{dialog_id}/messages", body)
    assert events[-1] == ("done", {"status": "complete"})
    last_run = [data for name, data in events if name == "sql_run"][-1]
    assert [block["status"] for block in last_run["blocks"]] == ["passed", "unavailable", "passed"]
    saved = (await _answer(client, dialog_id))["sql_run"]
    assert saved == last_run and saved["blocks"][0]["result"] is not None


async def test_test_run_is_refused_only_for_a_new_question(
    settings: Settings, portal: Portal
) -> None:
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    path = f"/api/dialogs/{dialog_id}/messages"
    # Флаг выключен: новый вопрос с проверкой — отказ, ничего не сохранено.
    refused = await client.post(path, json=body)
    assert (refused.status_code, refused.json()["error"]["code"]) == (403, "feature_disabled")
    assert await portal.rows("SELECT 1 FROM messages") == []

    # Вопрос, сохранённый с проверкой до выключения флага, отвечается заново без неё.
    await post_events(client, path, {**body, "test_run": False})
    await portal.execute("""UPDATE messages SET params = params || '{"test_run": true}'""")
    again = parse_events((await client.post(f"/api/dialogs/{dialog_id}/regenerate")).text)
    assert "sql_run" not in dict(again) and again[-1] == ("done", {"status": "complete"})
    stored = await portal.rows("SELECT params FROM messages WHERE role = 'user'")
    assert stored[0].params["test_run"] is True

    enabled = await make_portal(_enabled(settings), sql_sandbox=FakeSandbox())
    try:
        client = await enabled.employee()
        dialog_id, body = await _dialog(client)
        without_schema = await client.post(
            f"/api/dialogs/{dialog_id}/messages", json={**body, "schema_id": None}
        )
        assert without_schema.status_code == 422
        fields = without_schema.json()["error"]["fields"]
        assert [(item["field"], item["code"]) for item in fields] == [
            ("test_run", "invalid_format")
        ]
    finally:
        await close_portal(enabled)


async def test_lost_session_stops_the_check_and_keeps_the_answer(
    checked: tuple[Portal, FakeSandbox],
) -> None:
    portal, sandbox = checked
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    sandbox.script = [asyncio.Event()]
    _reply(portal, _sql("SELECT 1", "SELECT 2"))
    task = asyncio.create_task(post_events(client, f"/api/dialogs/{dialog_id}/messages", body))
    await asyncio.wait_for(sandbox.started.wait(), timeout=5)

    # Пока идёт проверка, диалог занят, хотя ответ уже сохранён (§5.5).
    row = (await portal.rows("SELECT status, sql_run FROM messages WHERE role = 'assistant'"))[0]
    assert (row.status, row.sql_run) == ("complete", None)
    busy = await client.post(f"/api/dialogs/{dialog_id}/regenerate")
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "generation_in_progress")

    actor = await portal.actor()
    await portal.container.admin.set_blocked(actor, await portal.user_id("ivanov"), blocked=True)
    events = await asyncio.wait_for(task, timeout=5)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "session_ended"
    assert "done" not in dict(events)
    row = (await portal.rows("SELECT * FROM messages WHERE role = 'assistant'"))[0]
    assert (row.status, row.error_code) == ("complete", None)
    assert [block["status"] for block in row.sql_run["blocks"]] == ["stopped", "stopped"]


async def test_closing_the_stream_stops_the_check_and_frees_the_dialog(
    checked: tuple[Portal, FakeSandbox],
) -> None:
    portal, sandbox = checked
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    path = f"/api/dialogs/{dialog_id}/messages"
    sandbox.script = [asyncio.Event()]
    _reply(portal, _sql("SELECT 1"))
    headers = {"X-Portal-Csrf": "1", "Cookie": f"portal_session={client.cookies['portal_session']}"}
    async with (
        live_server(portal.app) as url,
        httpx.AsyncClient(base_url=url, headers=headers, timeout=10) as live,
    ):
        async with live.stream("POST", path, json=body) as response:
            received = ""
            async for chunk in response.aiter_text():
                received += chunk
                if '"running"' in received:
                    break
        # Остановка — закрытие соединения; после неё вопрос принимается без отказа.
        _reply(portal, "Без запросов.")
        events = await post_events(client, path, {**body, "test_run": False})
    assert events[-1] == ("done", {"status": "complete"})
    assert "done" not in dict(parse_events(received))
    rows = await portal.rows(
        "SELECT status, sql_run FROM messages WHERE role = 'assistant' ORDER BY position"
    )
    assert rows[0].status == "complete"
    assert [block["status"] for block in rows[0].sql_run["blocks"]] == ["stopped"]


async def test_deleted_dialog_stops_the_check(checked: tuple[Portal, FakeSandbox]) -> None:
    portal, sandbox = checked
    client = await portal.employee()
    dialog_id, body = await _dialog(client)
    sandbox.script = [asyncio.Event()]
    _reply(portal, _sql("SELECT 1"))
    task = asyncio.create_task(post_events(client, f"/api/dialogs/{dialog_id}/messages", body))
    await asyncio.wait_for(sandbox.started.wait(), timeout=5)
    await portal.execute("DELETE FROM dialogs")
    events = await asyncio.wait_for(task, timeout=5)
    assert events[-1][0] == "error" and events[-1][1]["code"] == "dialog_deleted"
