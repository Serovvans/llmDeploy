"""Проверка SQL: что считается запросом, опасные операции, синтаксис (docs/portal-api.md §7.1)."""

import asyncio
import re
from pathlib import Path

import pytest

from portal.dialogs.markdown import Code, Heading, ListBlock, Paragraph, Table, parse_blocks
from portal.tools.sql_check import (
    DangerRules,
    SyntaxVerdict,
    answer_sql_blocks,
    find_dangers,
    question_dangers,
    question_queries,
)
from portal.tools.sql_syntax import SqlSyntaxChecker, check_syntax

pytestmark = pytest.mark.anyio


def test_what_counts_as_a_query_in_the_question() -> None:
    # Есть блоки кода: каждый блок с языком sql или без языка.
    content = (
        "Объясни:\n```sql\nDELETE FROM a\n```\nи ещё\n```\nDROP TABLE b\n```\n"
        "```python\nprint('DROP')\n```"
    )
    assert question_queries(content) == ["DELETE FROM a", "DROP TABLE b"]
    assert question_dangers(content) == ["drop", "delete_without_where"]
    # Блоки есть, но ни один не запрос.
    assert question_dangers("```python\nprint(1)\n```") is None
    # Блоков нет: весь текст, если он начинается с ключевого слова оператора.
    assert question_dangers("  -- правка\n  update parcels set area = 1") == [
        "update_without_where"
    ]
    assert question_dangers("SELECT * FROM parcels") == []
    for keyword in ("WITH", "INSERT", "MERGE", "CREATE", "ALTER", "TRUNCATE", "DROP"):
        assert question_queries(f"{keyword} something") is not None
    # Уточняющий вопрос словами: проверять нечего.
    assert question_dangers("А почему запрос удаляет всё? Там же DELETE FROM parcels") is None
    assert question_dangers("Удали таблицу: DROP TABLE parcels") is None
    assert question_dangers("") is None
    # Незакрытый блок в вопросе длится до конца текста.
    assert question_dangers("```sql\nTRUNCATE parcels") == ["truncate"]


def test_sql_blocks_of_the_answer_are_numbered_among_sql_blocks() -> None:
    answer = (
        "Вот запрос:\n```SQL\nSELECT 1\n```\n```csharp\nvar x = 1;\n```\n"
        "~~~sql\nDELETE FROM t\n~~~\nи оборванный\n```sql\nUPDATE t SET"
    )
    blocks = answer_sql_blocks(answer)
    assert [(block.index, block.text, block.closed) for block in blocks] == [
        (0, "SELECT 1", True),
        (1, "DELETE FROM t", True),
        (2, "UPDATE t SET", False),
    ]
    assert answer_sql_blocks("Запроса нет, только текст и `SELECT` в строке.") == []


def _verdict(sql: str) -> SyntaxVerdict:
    verdict = check_syntax(sql, "postgres")
    assert isinstance(verdict, SyntaxVerdict)
    return verdict


def test_syntax_verdict_gives_place_not_library_message() -> None:
    assert _verdict("SELECT id, ST_Area(geom) FROM parcels WHERE id = 1").valid
    broken = _verdict("SELECT *\nFROM parcels\nWHERE (area = ")
    assert not broken.valid
    assert (broken.line, broken.near) == (3, "=") and broken.column and broken.column > 1
    typo = _verdict("SELECT * FORM parcels")
    assert (typo.valid, typo.line, typo.near) == (False, 1, "parcels")
    unterminated = _verdict("SELECT 'abc")
    assert not unterminated.valid and unterminated.line == 1
    assert len(unterminated.near or "") <= 80


def test_pathological_sql_does_not_crash_the_parser() -> None:
    """Глубокая вложенность не роняет процесс: синтаксис просто не проверен."""
    assert check_syntax("SELECT " + "(" * 5000 + "1" + ")" * 5000, "postgres") == "too_complex"
    garbage = check_syntax("\x00\x01 ;;;; ))) ''' \"\"\" $$", "postgres")
    assert garbage == "too_complex" or (isinstance(garbage, SyntaxVerdict) and not garbage.valid)


async def test_checker_limits_length_and_time(monkeypatch: pytest.MonkeyPatch) -> None:
    checker = SqlSyntaxChecker(max_chars=100, timeout_seconds=0.2)
    try:
        verdict = await checker.check("SELECT 1", "postgres")
        assert isinstance(verdict, SyntaxVerdict) and verdict.valid
        assert await checker.check("SELECT " + "1, " * 100 + "1", "postgres") == "too_large"

        import time

        from portal.tools import sql_syntax

        monkeypatch.setattr(sql_syntax, "check_syntax", lambda sql, dialect: time.sleep(0.6))
        started = asyncio.get_running_loop().time()
        assert await checker.check("SELECT 1", "postgres") == "too_complex"
        assert asyncio.get_running_loop().time() - started < 0.5
    finally:
        checker.close()


def _shipped_rules(backslash_escapes: bool = False) -> DangerRules:
    from portal.core.settings import load_settings
    from tests.conftest import CONFIG_DIR

    settings = load_settings({"PORTAL_DB_PASSWORD": "x"}, CONFIG_DIR, Path("/nonexistent"))
    patterns = tuple(re.compile(p, re.IGNORECASE) for p in settings.sql.error_line_patterns)
    assert len(patterns) == 6
    return DangerRules(patterns, backslash_escapes)


RULES = _shipped_rules()
WARN_DELETE = ["delete_without_where"]
WARN_UPDATE = ["update_without_where"]


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        # Шаг 1: текст ошибки СУБД отбрасывается, слово WHERE в нём предупреждение не скрывает.
        ("DELETE FROM t\nERROR: syntax error at or near where", WARN_DELETE),
        ("DELETE FROM t\n  ОШИБКА:  ошибка синтаксиса (положение: where)", WARN_DELETE),
        ("UPDATE t SET a = 1\nLINE 1: UPDATE t SET a = 1 where", WARN_UPDATE),
        ("DELETE FROM t\nSQL state: 42601 where", WARN_DELETE),
        ("DELETE FROM t\nSQLSTATE[42000]: where", WARN_DELETE),
        ("DELETE FROM t\nORA-00933: command not properly ended where x", WARN_DELETE),
        ("DELETE FROM t\n\nMsg 102, Level 15: Incorrect near where", WARN_DELETE),
        ("DELETE FROM t\nSQL Error [42601]: where", WARN_DELETE),
        ("DELETE FROM t\nCaused by: org.postgresql.PSQLException where", WARN_DELETE),
        ("DELETE FROM t\nHINT: добавьте where\nDETAIL: where", WARN_DELETE),
        # Всё после первой строки-сообщения в поиске не участвует.
        ("SELECT 1\nERROR: bad\nLINE 1: DROP TABLE t", []),
        # Одного слова в начале строки мало: столбцы отформатированного запроса — не ошибка.
        ("UPDATE t SET\n  hint = 1,\n  detail = 2,\n  line = 3\nWHERE id = 1", []),
        ("UPDATE t\nSET x = 1\nWHERE\n  error::text = 'x'", []),
        ("DELETE FROM t\nWHERE\n  строка = 5\n  AND line int", []),
        ("SELECT\n  detail,\n  hint\nFROM t;\nDELETE FROM t\nWHERE\n  error = 1", []),
        # Строка-сообщение ищется только вне непрозрачных участков: внутри строки,
        # комментария или долларовых кавычек она текст не обрывает.
        ("SELECT 'a\nERROR: b'; DROP TABLE t", ["drop"]),
        ("/* \nHINT: x */ DROP TABLE t", ["drop"]),
        ("SELECT $$\nline 1: x\n$$; TRUNCATE t", ["truncate"]),
        ("SELECT 1 -- заметка\nERROR: bad; DROP TABLE t", []),
        ("ERROR: relation does not exist\nDROP TABLE t", []),
        ("DELETE FROM t\nSQLSTATE 42601 where", WARN_DELETE),
        ("DELETE FROM t\nSQL state [42601] where", WARN_DELETE),
        # Пустая строка оператор не обрывает.
        ("DELETE FROM parcels\n\nWHERE id = 1", []),
        ("UPDATE parcels\nSET area = 1\n\n\n  WHERE id = 1\n\nERROR: no such relation", []),
        # Шаг 2: непрозрачные участки не скрывают и не вызывают предупреждение.
        ("DELETE FROM t -- WHERE id = 1", WARN_DELETE),
        ("DELETE FROM t /* WHERE id = 1 */", WARN_DELETE),
        ("DELETE FROM t /* вложенный /* WHERE */ комментарий WHERE */", WARN_DELETE),
        ("DELETE FROM t RETURNING 'WHERE'", WARN_DELETE),
        ("DELETE FROM t RETURNING $$ WHERE $$, $tag$ WHERE $tag$", WARN_DELETE),
        ('DELETE FROM t RETURNING "where", `where`', WARN_DELETE),
        ("SELECT 'DELETE FROM t; DROP TABLE x'", []),
        ("SELECT $$ DELETE FROM t; $$; SELECT $f$ DROP TABLE x $f$", []),
        ("/* DELETE FROM t */ SELECT 1 -- DROP TABLE x", []),
        ('SELECT "delete", `drop` FROM "truncate"', []),
        ("SELECT 'it''s; DROP TABLE x' FROM t", []),
        ("SELECT '(' ; DELETE FROM t WHERE id = 1", []),
        # Строка E'…': обратная черта экранирует кавычку.
        ("UPDATE t SET a = E'it\\'s' WHERE id=1", []),
        ("UPDATE t SET a = e'\\\\' WHERE id = 1; DELETE FROM u", WARN_DELETE),
        # В обычной строке PostgreSQL обратная черта — обычный символ.
        ("UPDATE t SET a = 'c:\\' WHERE id = 1", []),
        # Незакрытый участок длится до конца текста.
        ("DELETE FROM t 'незакрытая строка WHERE", WARN_DELETE),
        ("SELECT 1 /* незакрытый комментарий; DROP TABLE x", []),
        ("SELECT $$ незакрытые доллары; DROP TABLE x", []),
        # Шаг 3: операторы делит точка с запятой.
        ("BEGIN; DELETE FROM t;", WARN_DELETE),
        ("BEGIN; DELETE FROM t WHERE id = 1; COMMIT;", []),
        ("UPDATE t SET a = 1; DELETE FROM t; TRUNCATE t; DROP TABLE t; DELETE FROM u",
         ["drop", "truncate", "delete_without_where", "update_without_where"]),
        # Шаг 4: главное слово оператора.
        ("EXPLAIN ANALYZE DELETE FROM t", WARN_DELETE),
        ("EXPLAIN (ANALYZE, BUFFERS) UPDATE t SET a = 1", WARN_UPDATE),
        ("explain verbose delete from t where id = 1", []),
        ("WITH old AS (SELECT id FROM t WHERE y < 2000) DELETE FROM t", WARN_DELETE),
        ("WITH old AS (SELECT 1) DELETE FROM t WHERE id IN (SELECT 1)", []),
        # Шаг 5.
        ("DROP TABLE parcels", ["drop"]),
        ("TRUNCATE parcels", ["truncate"]),
        ("UPDATE parcels SET owner = (SELECT name FROM o WHERE id = 1)", WARN_UPDATE),
        ("DELETE FROM parcels USING (SELECT 1 WHERE true) s", WARN_DELETE),
        # Шаг 6: изменяющий оператор в скобках — отдельный оператор.
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", WARN_DELETE),
        ("WITH d AS (DELETE FROM t WHERE id = 1 RETURNING *) SELECT * FROM d WHERE x", []),
        ("WITH u AS (UPDATE t SET a = 1 RETURNING *) SELECT * FROM u WHERE a = 1", WARN_UPDATE),
        ("WITH d AS (DELETE FROM t WHERE id IN (SELECT 1 WHERE true)) SELECT 1", []),
        ("WITH d AS (DELETE FROM t", WARN_DELETE),
        # Чего правила не задевают.
        ("INSERT INTO t VALUES (1) ON CONFLICT (id) DO UPDATE SET a = 1", []),
        ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", []),
        ("SELECT * FROM t FOR UPDATE", []),
        ("GRANT UPDATE, DELETE ON t TO bob", []),
        ("CREATE TRIGGER tr AFTER UPDATE OR DELETE ON t EXECUTE FUNCTION f()", []),
        ("ALTER TABLE parcels DROP COLUMN note", []),
        # Запрос с ошибкой синтаксиса тоже получает предупреждение.
        ("DELETE FORM parcels", WARN_DELETE),
        ("", []),
        ("((((", []),
        ("))))", []),
    ],
)  # fmt: skip
def test_dangerous_operations_step_by_step(sql: str, expected: list[str]) -> None:
    assert find_dangers([sql], RULES) == expected


def test_backslash_in_plain_strings_follows_the_dialect() -> None:
    sql = "UPDATE t SET a = 'it\\'s' WHERE id = 1"
    # Там, где обратная черта экранирует и в обычной строке, кавычка строку не закрывает.
    assert find_dangers([sql], _shipped_rules(backslash_escapes=True)) == []
    checker = SqlSyntaxChecker(max_chars=100, timeout_seconds=1)
    try:
        assert checker.backslash_escapes("mysql") is True
        assert checker.backslash_escapes("postgres") is False
    finally:
        checker.close()


def test_question_starting_with_a_wider_set_of_words_is_a_query() -> None:
    assert question_dangers("EXPLAIN ANALYZE DELETE FROM t", RULES) == WARN_DELETE
    assert question_dangers("BEGIN; DELETE FROM t;", RULES) == WARN_DELETE
    nested = "/* правка /* вложенный */ */ begin;\nupdate t set a = 1"
    assert question_dangers(nested, RULES) == WARN_UPDATE
    for word in ("START", "COMMIT", "ROLLBACK", "SET", "LOCK", "GRANT", "REVOKE", "CALL", "DO"):
        assert question_dangers(f"{word} something", RULES) == []
    assert question_dangers("VALUES (1)", RULES) == []
    assert question_dangers("COMMENT ON TABLE t IS 'x'; DROP TABLE t", RULES) == ["drop"]
    # Запрос в скобках — тоже запрос.
    assert question_dangers("(DELETE FROM t)", RULES) == WARN_DELETE
    assert question_dangers("  /* a */ ( -- b\n (select 1)); truncate t", RULES) == ["truncate"]
    assert question_dangers("(см. запрос ниже) DELETE FROM t", RULES) is None
    assert question_dangers("(", RULES) is None
    assert question_dangers("Почему BEGIN; DELETE FROM t; удаляет всё?", RULES) is None


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM t WHERE x = '" + "a" * 2_000_000,  # незакрытая строка
        "SELECT 1 /* " + "/* " * 300_000,  # незакрытые вложенные комментарии
        "SELECT " + "(" * 500_000 + "1",  # глубокая вложенность скобок
        "SELECT " + "(" * 200_000 + "DELETE FROM t" + ")" * 200_000,
        "DELETE FROM t WHERE id IN (" + ", ".join(["1"] * 300_000) + ")",  # длинная строка
        "'" * 1_000_001,
        "$a$" + "$" * 500_000,
        ("-- комментарий\n" * 100_000) + "DROP TABLE t",
        "e'" + "\\'" * 500_000,
        ";" * 1_000_000,
        "\n".join(["hint = 1,"] * 100_000),  # много строк на проверку шаблонами
    ],
)
def test_danger_scan_is_linear_and_survives_pathological_input(sql: str) -> None:
    """Разбор не зависает и не падает: время линейно по длине текста."""
    import time

    started = time.perf_counter()
    result = find_dangers([sql], RULES)
    assert time.perf_counter() - started < 3
    assert isinstance(result, list)


# Начала строк, с которых шаблоны из конфигурации начинают разбирать сообщение СУБД.
ERROR_LINE_PREFIXES = (
    "error", "ошибка", "hint", "caused by", "line", "строка", "line 1", "sql", "sql state",
    "sqlstate", "sql state:", "sqlstate[", "ora-", "ora-1234", "msg", "msg 1", "sql error",
)  # fmt: skip


@pytest.mark.parametrize("prefix", ERROR_LINE_PREFIXES)
@pytest.mark.parametrize("filler", [" ", "\t", "1", "a", ":", " :", "[ "])
def test_shipped_error_line_patterns_do_not_backtrack(prefix: str, filler: str) -> None:
    """Длинная строка после каждого префикса: шаблоны из конфигурации её не перебирают."""
    import time

    line = prefix + (filler * 32_000)[:32_000] + "!"
    # Предел длины начала строки здесь снят: линейны сами поставляемые шаблоны.
    rules = DangerRules(RULES.error_line_patterns, False, error_line_check_chars=len(line))
    started = time.perf_counter()
    find_dangers(["DELETE FROM t\n" + line], rules)
    assert time.perf_counter() - started < 0.5


def test_error_line_patterns_see_only_the_start_of_a_line() -> None:
    """Шаблон администратора с перебором не задерживает разбор длинной строки."""
    import time

    quadratic = re.compile(r"sql\s*state\s*[:\[]?\s*[0-9a-z]{5}\b", re.IGNORECASE)
    sql = "DELETE FROM t\nsql state" + " " * 32_000 + "!"
    started = time.perf_counter()
    assert find_dangers([sql], DangerRules((quadratic,), False, 200)) == WARN_DELETE
    assert time.perf_counter() - started < 0.5
    # Сообщение, которое начинается дальше предела, шаблон не видит.
    shifted = "DELETE FROM t\nSQL" + " " * 300 + "state: 42601 where"
    assert find_dangers([shifted], DangerRules((quadratic,), False, 200)) == []
    assert find_dangers([shifted], DangerRules((quadratic,), False, 400)) == WARN_DELETE


def test_invalid_error_line_pattern_stops_startup(tmp_path: Path) -> None:
    import yaml

    from portal.core.settings import ConfigError, load_settings
    from tests.conftest import CONFIG_DIR

    override = tmp_path / "config.override.yaml"
    override.write_text(yaml.safe_dump({"sql": {"error_line_patterns": ["(незакрытая"]}}))
    with pytest.raises(ConfigError, match="error_line_patterns"):
        load_settings({"PORTAL_DB_PASSWORD": "x"}, CONFIG_DIR, override)


def test_markdown_blocks() -> None:
    text = (
        "# Заголовок\n\nПервый абзац\nв две строки.\n\n- раз\n- два\n  продолжение\n\n"
        "1. один\n2) два\n\n| a | b |\n|---|:-:|\n| 1 | 2 |\n\n"
        "```py\nx = 1\n\n# не заголовок\n```\n"
        "<b>не html</b>\n~~~\nоборван"
    )
    assert parse_blocks(text) == [
        Heading(1, "Заголовок"),
        Paragraph("Первый абзац\nв две строки."),
        ListBlock(False, ("раз", "два продолжение")),
        ListBlock(True, ("один", "два")),
        Table((("a", "b"), ("1", "2"))),
        Code("py", "x = 1\n\n# не заголовок", True, 17),
        Paragraph("<b>не html</b>"),
        Code("", "оборван", False, 23),
    ]
    assert parse_blocks("") == []


def test_fence_rule_matches_the_contract() -> None:
    """Шесть шагов §7.1 «Что сервер считает блоком sql»: на них опирается интерфейс."""

    def found(text: str) -> list[tuple[int, int, str, bool]]:
        return [(b.index, b.line, b.text, b.closed) for b in answer_sql_blocks(text)]

    # Шаг 1: до трёх пробелов отступа; четыре пробела, цитата и маркер списка — не блок.
    assert found("   ```sql\nA\n```") == [(0, 1, "A", True)]
    assert found("~~~~sql\nA\n~~~~") == [(0, 1, "A", True)]
    assert found("    ```sql\nA\n    ```") == []
    assert found("> ```sql\n> A\n> ```") == []
    assert found("- ```sql\n  A\n  ```") == []
    # Шаг 2: язык — слово после ограждения, регистр не важен.
    for opening in ("```SQL", "``` sql", "```sql title", "```sql`x"):
        assert found(f"{opening}\nA\n```") == [(0, 1, "A", True)], opening
    for opening in ("```sqlite", "```postgresql", "```", "```python"):
        assert found(f"{opening}\nA\n```") == [], opening
    # Шаг 3: закрывает строка только из знаков того же вида, числом не меньше.
    assert found("````sql\n```\nA\n~~~~\n`````  \nB") == [(0, 1, "```\nA\n~~~~", True)]
    assert found("```sql\nA\n``` sql\n    ```\n```") == [(0, 1, "A\n``` sql\n    ```", True)]
    # Шаг 4: незакрытый блок длится до конца и участвует в счёте.
    assert found("```sql\nA\n```\n\n```sql\nB") == [(0, 1, "A", True), (1, 5, "B", False)]
    # Шаг 5: ограждение прерывает пункт списка; с отступом в четыре пробела — нет.
    assert found("1. пункт\n```sql\nA\n```\n\n```sql\nB\n```") == [
        (0, 2, "A", True),
        (1, 6, "B", True),
    ]
    assert found("1. пункт\n    ```sql\n    A\n    ```") == []
    assert found("- пункт\n\t```sql\n\tA\n\t```") == []
    assert found("> 1. пункт\n> ```sql\n> A\n> ```") == []
    # Шаг 6: блоки с другим языком и без языка в счёт не входят.
    assert found("```\nA\n```\n```py\nB\n```\n```sql\nC\n```") == [(0, 7, "C", True)]
    # Строка таблицы с ограждением открывает блок, а не становится строкой таблицы.
    assert found("| a | b |\n|---|---|\n```sql | x\nA\n```") == [(0, 3, "A", True)]
    # Перевод строки Windows: `\r` остаётся в конце строк и блоку не мешает.
    assert found("текст\r\n```sql\r\nA\r\n```\r\n") == [(0, 2, "A\r", True)]


@pytest.mark.parametrize(
    ("answer", "text", "dangers"),
    [
        (
            "1. Очистите таблицу:\n   ```sql\n   DROP TABLE t;\n   ```\n2. Готово",
            "   DROP TABLE t;", ["drop"],
        ),
        ("1. Очистите таблицу:\n```sql\nDROP TABLE t;\n```", "DROP TABLE t;", ["drop"]),
        ("- Вариант:\n  ```sql\n  DELETE FROM t;\n  ```", "  DELETE FROM t;", WARN_DELETE),
    ],
)  # fmt: skip
def test_block_right_under_a_list_item_is_checked(
    answer: str, text: str, dangers: list[str]
) -> None:
    """Ответ «по шагам»: блок сразу под пунктом списка, без пустой строки."""
    blocks = answer_sql_blocks(answer)
    assert [(b.index, b.line, b.text, b.closed) for b in blocks] == [(0, 2, text, True)]
    assert find_dangers([blocks[0].text], RULES) == dangers
    # В вопросе пользователя блоки ищет тот же разбор.
    assert question_queries(answer) == [text]
    assert question_dangers(answer, RULES) == dangers


def test_list_goes_on_after_a_block_under_its_item() -> None:
    text = "1. Очистите:\n   ```sql\n   DROP TABLE t;\n   ```\n2. Готово\n   и ещё\n- маркер"
    # Вложенность и вид списка разбор не различает: вид задаёт первый пункт.
    assert parse_blocks(text) == [
        ListBlock(True, ("Очистите:",)),
        Code("sql", "   DROP TABLE t;", True, 2),
        ListBlock(True, ("Готово и ещё", "маркер")),
    ]
    # Чередование пунктов и блоков: каждый блок получает свою строку.
    steps = "1. a\n```sql\nDROP TABLE a\n```\n2. b\n~~~sql\nTRUNCATE b\n~~~\n3. c"
    assert [(b.index, b.line, b.text) for b in answer_sql_blocks(steps)] == [
        (0, 2, "DROP TABLE a"),
        (1, 6, "TRUNCATE b"),
    ]


def test_footnote_rule_has_its_own_code_detection() -> None:
    """Отбор сносок разбором блоков не пользуется: его правило правкой списков не задето."""
    from portal.dialogs.footnotes import cited_numbers

    text = "1. Шаг [1]:\n   ```sql\n   SELECT a[2] FROM t;\n   ```\n2. Готово [3]\n    отступ [4]"
    assert cited_numbers(text, {1, 2, 3, 4}) == {1, 3}


def test_block_inside_a_list_item_is_skipped_and_the_next_gets_its_line() -> None:
    """Пример из контракта: нумерованный список с десятого пункта."""
    answer = (
        "9. Сначала посмотрите строки:\n"  # 1
        "10. Затем выполните:\n"  # 2
        "    ```sql\n"  # 3: отступ в четыре пробела — не ограждение
        "    SELECT * FROM users;\n"  # 4
        "    ```\n"  # 5
        "\n"  # 6
        "```sql\n"  # 7
        "DROP TABLE users\n"  # 8
        "```\n"  # 9
    )
    blocks = answer_sql_blocks(answer)
    assert [(b.index, b.line, b.text) for b in blocks] == [(0, 7, "DROP TABLE users")]
    assert find_dangers([blocks[0].text], RULES) == ["drop"]
