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
from sqlglot.errors import ParseError, SqlglotError, TokenError

from portal.tools.sql_check import SyntaxVerdict, Unchecked

_NEAR_MAX_CHARS = 80

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

    def backslash_escapes(self, dialect: str) -> bool:
        """Экранирует ли обратная черта в обычных строках диалекта — по его описанию."""
        return "\\" in sqlglot.Dialect.get_or_raise(dialect).tokenizer_class.STRING_ESCAPES

    def close(self) -> None:
        """Освободить поток разбора при остановке процесса."""
        self._executor.shutdown(wait=False, cancel_futures=True)
