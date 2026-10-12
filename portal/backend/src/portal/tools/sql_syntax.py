"""Синтаксическая проверка запроса библиотекой `sqlglot` (docs/portal-api.md §7.1).

Запрос — недоверенный текст из ответа модели. Разбор не должен ни уронить процесс, ни
занять его надолго: длина ограничена, разбор идёт в отдельном потоке (одном на
процесс) с пределом времени, слишком глубокая вложенность — «не проверено». Английские
сообщения библиотеки наружу не идут: только строка, столбец и фрагмент запроса.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, SqlglotError, TokenError

from portal.tools.sql_check import SyntaxVerdict, Unchecked
from portal.tools.sql_run import ParsedSql, StatementKind

_NEAR_MAX_CHARS = 80
# Имена в этих схемах остаются как есть: схемы в песочнице есть, и запрос к ним без имени
# схемы перестал бы исполняться (§7.4, «Имена таблиц со схемой»).
_SYSTEM_SCHEMAS = frozenset({"pg_catalog", "information_schema"})
type _Cut = tuple[int, int]

# Библиотека пишет в журнал текст запроса, который не смогла разобрать («… contains
# unsupported syntax»). Запрос — содержимое ответа модели, в журналы оно не попадает
# (docs/portal-design.md §8), поэтому её журнал отключён целиком.
logging.getLogger("sqlglot").disabled = True


def _token_error_place(sql: str, error: TokenError) -> SyntaxVerdict:
    """Место ошибки разбора на лексемы: библиотека сообщает только обрывок запроса."""
    message = str(error)
    context = message.partition("'")[2].rpartition("'")[0]
    offset = sql.find(context) + len(context) if context and context in sql else 0
    before = sql[:offset]
    return SyntaxVerdict(
        valid=False,
        line=before.count("\n") + 1,
        column=len(before) - (before.rfind("\n") + 1) + 1 if before else 1,
        near=context[-_NEAR_MAX_CHARS:],
    )


def check_syntax(sql: str, dialect: str) -> SyntaxVerdict | Unchecked:
    """Разобрать запрос; слишком глубокая вложенность — `too_complex`, а не сбой процесса."""
    try:
        sqlglot.parse(sql, read=dialect)
    except ParseError as error:
        first = error.errors[0] if error.errors else {}
        return SyntaxVerdict(
            valid=False,
            line=int(first.get("line") or 1),
            column=int(first.get("col") or 1),
            near=str(first.get("highlight") or "")[:_NEAR_MAX_CHARS],
        )
    except TokenError as error:
        return _token_error_place(sql, error)
    except SqlglotError:
        return SyntaxVerdict(valid=False, line=1, column=1, near="")
    except RecursionError:
        return "too_complex"
    return SyntaxVerdict(valid=True)


def _statement_kind(statement: exp.Expr) -> StatementKind:
    """Вид оператора для проверки на тестовых данных (§7.4)."""
    if isinstance(statement, exp.Select | exp.SetOperation | exp.Subquery):
        # `SELECT … INTO таблица` создаёт таблицу: это не запрос на чтение. У объединения
        # признак лежит не в корне, а в одном из его `SELECT`.
        return "other" if statement.find(exp.Into) else "select"
    if isinstance(statement, exp.Insert):
        return "insert_values" if isinstance(statement.expression, exp.Values) else "insert"
    if isinstance(statement, exp.Update):
        return "update"
    if isinstance(statement, exp.Delete):
        return "delete"
    if isinstance(statement, exp.Merge):
        return "merge"
    if isinstance(statement, exp.Create):
        plain_table = statement.kind == "TABLE" and statement.expression is None
        return "create_table" if plain_table else "other"
    return "other"


def _name(identifier: exp.Identifier) -> str:
    """Имя по правилам идентификаторов PostgreSQL: без кавычек регистр не различается."""
    name = str(identifier.this)
    return name if identifier.quoted else name.lower()


def _qualifiers(node: exp.Expr) -> list[exp.Expr] | None:
    """База и схема перед именем таблицы; `None` — снимать нечего.

    Таблица — имя источника или цели оператора либо таблица в ссылке на столбец. Функция
    со схемой (`public.generate_series(…)` во `FROM`) и имена в системных схемах — не в счёт.
    """
    if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
        return None
    schema = node.args.get("db")
    if schema is None:
        return None
    if isinstance(schema, exp.Identifier) and _name(schema) in _SYSTEM_SCHEMAS:
        return None
    catalog = node.args.get("catalog")
    return [schema] if catalog is None else [catalog, schema]


def _cut(sql: str, qualifier: exp.Expr) -> _Cut | None:
    """Место имени схемы вместе с точкой после него; `None` — разбор места не дал."""
    # Пустая схема (`db..table`) приходит из разбора строкой, а не узлом.
    if not isinstance(qualifier, exp.Identifier):
        return None
    start, end = qualifier.meta.get("start"), qualifier.meta.get("end")
    if start is None or end is None:
        return None
    name = str(qualifier.this)
    written = '"' + name.replace('"', '""') + '"' if qualifier.quoted else name
    # Сверка с текстом: по неверному месту было бы вырезано что-то кроме имени схемы.
    if sql[start : end + 1] != written:
        return None
    dot = end + 1
    while dot < len(sql) and sql[dot].isspace():
        dot += 1
    return (start, dot + 1) if sql[dot : dot + 1] == "." else None


def _without_schemas(sql: str, statement: exp.Expr) -> str | None:
    """Текст оператора без имён схем у таблиц; `None` — место имени схемы не найдено.

    Из текста только вырезаются имена схем с точкой; из дерева разбора он не собирается.
    """
    cuts: set[_Cut] = set()
    for node in statement.find_all(exp.Table, exp.Column):
        for qualifier in _qualifiers(node) or ():
            cut = _cut(sql, qualifier)
            if cut is None:
                return None
            cuts.add(cut)
    for start, end in sorted(cuts, reverse=True):
        sql = sql[:start] + sql[end:]
    return sql


def _name_conflict(statement: exp.Expr) -> bool:
    """Стоит ли одно имя таблицы с разными схемами либо и со схемой, и без неё.

    Сравниваются упоминания таблиц как источников и целей оператора и имена общих
    табличных выражений; ссылки на столбцы и системные схемы в счёт не идут.
    """
    schemas: dict[str, set[tuple[str, ...]]] = {}
    for table in statement.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        parts = [table.args.get("catalog"), table.args.get("db")]
        qualifier = tuple(_name(part) for part in parts if isinstance(part, exp.Identifier))
        if not _SYSTEM_SCHEMAS.intersection(qualifier):
            schemas.setdefault(_name(table.this), set()).add(qualifier)
    for cte in statement.find_all(exp.CTE):
        alias = cte.args["alias"].this
        if isinstance(alias, exp.Identifier):
            schemas.setdefault(_name(alias), set()).add(())
    return any(len(qualifiers) > 1 for qualifiers in schemas.values())


def parse_statements(sql: str, dialect: str) -> ParsedSql | None:
    """Виды операторов текста, а у единственного оператора — текст без имён схем (§7.4).

    `None` — текст не разобран либо разбор не дал места имени схемы.
    """
    try:
        # Пустой оператор (лишняя `;` в конце) оператором не считается.
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
        kinds = [_statement_kind(statement) for statement in statements]
        if len(statements) != 1:
            return ParsedSql(kinds, sql, name_conflict=False)
        bare = _without_schemas(sql, statements[0])
        if bare is None:
            return None
        return ParsedSql(kinds, bare, _name_conflict(statements[0]))
    except (SqlglotError, RecursionError):
        return None


class SqlSyntaxChecker:
    """Реализация порта `SyntaxChecker`: пределы длины и времени, один разбор за раз."""

    def __init__(self, max_chars: int, timeout_seconds: float) -> None:
        """Запомнить пределы; поток разбора — один на процесс."""
        self._max_chars = max_chars
        self._timeout_seconds = timeout_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sql-check")

    async def check(self, sql: str, dialect: str) -> SyntaxVerdict | Unchecked:
        """Итог разбора либо причина, по которой синтаксис не проверялся."""
        if len(sql) > self._max_chars:
            return "too_large"
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._executor, check_syntax, sql, dialect),
                self._timeout_seconds,
            )
        except TimeoutError:
            return "too_complex"

    async def statements(self, sql: str, dialect: str) -> ParsedSql | None:
        """Разбор текста до исполнения — с теми же пределами, что у `check`.

        `None` — текст не разобран либо превышен предел.
        """
        if len(sql) > self._max_chars:
            return None
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(self._executor, parse_statements, sql, dialect),
                self._timeout_seconds,
            )
        except TimeoutError:
            return None

    def backslash_escapes(self, dialect: str) -> bool:
        """Экранирует ли обратная черта в обычных строках диалекта — по его описанию."""
        return "\\" in sqlglot.Dialect.get_or_raise(dialect).tokenizer_class.STRING_ESCAPES

    def close(self) -> None:
        """Освободить поток разбора при остановке процесса."""
        self._executor.shutdown(wait=False, cancel_futures=True)
