"""Что в тексте считается запросом SQL и какие операции в нём опасны (§7.1); чистые функции.

Опасные операции определяются по ключевым словам оператора и не зависят от того,
разбирается ли запрос: предупреждение получает и запрос с ошибкой синтаксиса, и запрос с
дописанным к нему текстом ошибки СУБД.
"""

import re
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from portal.dialogs.markdown import Code, parse_blocks

# Порядок — порядок в ответе API.
DANGERS = ("drop", "truncate", "delete_without_where", "update_without_where")

# С каких слов начинается текст, который в вопросе считается запросом (§7.1).
_QUERY_FIRST_WORDS = frozenset(
    {
        "SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "ALTER", "DROP",
        "TRUNCATE", "EXPLAIN", "ANALYZE", "BEGIN", "START", "COMMIT", "ROLLBACK", "SET", "LOCK",
        "GRANT", "REVOKE", "CALL", "DO", "VALUES", "COMMENT",
    }
)  # fmt: skip
_MAIN_VERBS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE"})
_EXPLAIN_WORDS = frozenset({"EXPLAIN", "ANALYZE", "ANALYSE", "VERBOSE"})
_MODIFYING = {"DELETE": "delete_without_where", "UPDATE": "update_without_where"}
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
_COMMENT_EDGE = re.compile(r"/\*|\*/")


@dataclass(frozen=True)
class DangerRules:
    """Настройки поиска опасных операций.

    `error_line_patterns` — как выглядит начало строки сообщения СУБД: на первой такой
    строке вне непрозрачных участков текст запроса кончается. Шаблоны видят только первые
    `error_line_check_chars` символов строки: их задаёт администратор, и неудачное
    выражение не должно разбирать длинную строку долго. `backslash_escapes` — экранирует
    ли обратная черта в обычных строках диалекта (в строках `E'…'` — всегда).
    """

    error_line_patterns: Sequence[re.Pattern[str]] = ()
    backslash_escapes: bool = False
    error_line_check_chars: int = 200


_DEFAULT_RULES = DangerRules()


# Почему синтаксис блока не проверялся: блок слишком большой либо слишком сложный
# (разбор не уложился во время или запрос слишком глубоко вложен).
Unchecked = Literal["too_large", "too_complex"]


@dataclass(frozen=True)
class SyntaxVerdict:
    """Итог разбора запроса: `line`, `column`, `near` заданы, когда он не разобран."""

    valid: bool
    line: int | None = None
    column: int | None = None
    near: str | None = None


class SyntaxChecker(Protocol):
    """Синтаксическая проверка запроса; реализация — на `sqlglot`."""

    async def check(self, sql: str, dialect: str) -> SyntaxVerdict | Unchecked:
        """Итог разбора либо причина, по которой синтаксис не проверялся."""
        ...

    def backslash_escapes(self, dialect: str) -> bool:
        """Экранирует ли обратная черта в обычных строках этого диалекта."""
        ...


@dataclass(frozen=True)
class SqlBlock:
    """Блок `sql` в ответе модели: номер среди блоков этого языка, строка открытия, текст."""

    index: int
    line: int
    text: str
    closed: bool


def _error_line_at(sql: str, position: int, rules: DangerRules) -> bool:
    """Начинается ли в `position` (после пробелов начала строки) сообщение СУБД."""
    if not rules.error_line_patterns:
        return False
    start = sql[position : position + rules.error_line_check_chars]
    return any(pattern.match(start) for pattern in rules.error_line_patterns)


def _line_text_start(sql: str, position: int) -> int:
    """Первый символ строки, начатой в `position`, после пробелов и табуляций."""
    length = len(sql)
    while position < length and sql[position] in " \t":
        position += 1
    return position


def _skip_quoted(sql: str, position: int, quote: str, backslash: bool) -> int:
    """Конец участка в кавычках, открытого в `position`; незакрытый длится до конца."""
    position += 1
    length = len(sql)
    while position < length:
        char = sql[position]
        if backslash and char == "\\":
            position += 2
        elif char == quote:
            if sql.startswith(quote, position + 1):  # удвоенная кавычка — это кавычка
                position += 2
            else:
                return position + 1
        else:
            position += 1
    return length


def _skip_block_comment(sql: str, position: int) -> int:
    """Конец комментария `/* … */`, в том числе вложенного; незакрытый — до конца."""
    depth = 0
    for edge in _COMMENT_EDGE.finditer(sql, position):
        depth += 1 if edge[0] == "/*" else -1
        if depth == 0:
            return edge.end()
    return len(sql)


def _tokens(sql: str, rules: DangerRules) -> Iterator[str]:
    """Шаги 1–2: слова в верхнем регистре и знаки `(`, `)`, `;` вне непрозрачных участков.

    Разбор кончается на первой строке-сообщении СУБД, начатой вне непрозрачного участка.
    Каждый символ текста просматривается один раз: время разбора линейно по длине.
    """
    length = len(sql)
    position = _line_text_start(sql, 0)
    if _error_line_at(sql, position, rules):
        return
    while position < length:
        char = sql[position]
        if char == "\n":
            position = _line_text_start(sql, position + 1)
            if _error_line_at(sql, position, rules):
                return
        elif char == "'":
            position = _skip_quoted(sql, position, "'", rules.backslash_escapes)
        elif char in ('"', "`"):
            position = _skip_quoted(sql, position, char, backslash=False)
        elif sql.startswith("--", position):
            end = sql.find("\n", position)
            position = length if end < 0 else end
        elif sql.startswith("/*", position):
            position = _skip_block_comment(sql, position)
        elif char == "$" and (tag := _DOLLAR_TAG.match(sql, position)) is not None:
            end = sql.find(tag[0], tag.end())
            position = length if end < 0 else end + len(tag[0])
        elif char in "();":
            yield char
            position += 1
        elif (word := _WORD.match(sql, position)) is not None:
            position = word.end()
            if word[0] in ("E", "e") and sql.startswith("'", position):
                # Строка с приставкой E: обратная черта экранирует следующий символ.
                position = _skip_quoted(sql, position, "'", backslash=True)
            else:
                yield word[0].upper()
        else:
            position += 1


class _Statement:
    """Оператор верхнего уровня: главное слово и наличие `WHERE` вне скобок (шаги 4–5)."""

    def __init__(self) -> None:
        self._stage = "prefix"
        self._main = ""
        self._where = False

    def word(self, word: str) -> None:
        if self._stage == "prefix":
            # EXPLAIN с его словами пропускается; список в скобках сюда не попадает.
            if word in _EXPLAIN_WORDS:
                return
            if word == "WITH":
                self._stage = "with"
            else:
                self._main, self._stage = word, "body"
        elif self._stage == "with":
            if word in _MAIN_VERBS:
                self._main, self._stage = word, "body"
        elif word == "WHERE":
            self._where = True

    def danger(self) -> str | None:
        if self._main == "DROP":
            return "drop"
        if self._main == "TRUNCATE":
            return "truncate"
        return None if self._where else _MODIFYING.get(self._main)


def _query_dangers(sql: str, rules: DangerRules) -> Iterator[str]:
    """Опасные операции одного текста запроса."""
    statement = _Statement()
    # Открытые скобки: первое слово внутри и встретилось ли `WHERE` на их уровне (шаг 6).
    groups: list[list[str | bool]] = []

    def close_group() -> str | None:
        first, where = groups.pop()
        return None if where else _MODIFYING.get(str(first))

    for token in _tokens(sql, rules):
        if token == "(":
            groups.append(["", False])
        elif token == ")":
            if groups and (danger := close_group()) is not None:
                yield danger
        elif token == ";":
            # Конец оператора закрывает и скобки, оставшиеся незакрытыми.
            while groups:
                if (danger := close_group()) is not None:
                    yield danger
            if (danger := statement.danger()) is not None:
                yield danger
            statement = _Statement()
        elif groups:
            group = groups[-1]
            if not group[0]:
                group[0] = token
            elif token == "WHERE":
                group[1] = True
        else:
            statement.word(token)
    while groups:
        if (danger := close_group()) is not None:
            yield danger
    if (danger := statement.danger()) is not None:
        yield danger


def _leading_word(text: str) -> str:
    """Первое слово текста в верхнем регистре.

    Пробелы и комментарии в начале пропускаются; открывающие скобки перед словом — тоже:
    `(DELETE FROM t)` — запрос.
    """
    position, length = 0, len(text)
    while position < length:
        if text[position].isspace() or text[position] == "(":
            position += 1
        elif text.startswith("--", position):
            end = text.find("\n", position)
            position = length if end < 0 else end
        elif text.startswith("/*", position):
            position = _skip_block_comment(text, position)
        else:
            break
    word = _WORD.match(text, position)
    return word[0].upper() if word is not None else ""


def find_dangers(queries: Iterable[str], rules: DangerRules = _DEFAULT_RULES) -> list[str]:
    """Опасные операции во всех операторах запросов: без повторов, в порядке `DANGERS`.

    Собственный разбор по ключевым словам, шаги — в §7.1 контракта. Это предупреждение,
    а не защита: результат не зависит от того, разбирается ли запрос.
    """
    found = {danger for query in queries for danger in _query_dangers(query, rules)}
    return [danger for danger in DANGERS if danger in found]


def question_queries(content: str) -> list[str] | None:
    """Что в вопросе пользователя считается запросом; `None` — проверять нечего.

    Если в тексте есть блоки кода — каждый блок с языком `sql` или без языка; иначе весь
    текст, если он начинается с ключевого слова оператора.
    """
    blocks = [block for block in parse_blocks(content) if isinstance(block, Code)]
    if blocks:
        queries = [block.text for block in blocks if block.language in ("", "sql")]
        return queries or None
    return [content] if _leading_word(content) in _QUERY_FIRST_WORDS else None


def question_dangers(content: str, rules: DangerRules = _DEFAULT_RULES) -> list[str] | None:
    """`sql_dangers` вопроса: `[]` — запрос есть, опасного нет; `None` — запроса нет."""
    queries = question_queries(content)
    return None if queries is None else find_dangers(queries, rules)


def answer_sql_blocks(content: str) -> list[SqlBlock]:
    """Блоки кода с языком `sql` в ответе модели, с номерами среди таких блоков."""
    blocks = [
        block
        for block in parse_blocks(content)
        if isinstance(block, Code) and block.language == "sql"
    ]
    return [
        SqlBlock(index, block.line, block.text, block.closed) for index, block in enumerate(blocks)
    ]
