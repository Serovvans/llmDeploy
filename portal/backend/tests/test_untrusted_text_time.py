"""Разборы недоверенного текста линейны: вход предельной длины не задерживает процесс.

Процесс портала один, а `re` не отпускает GIL: выражение, которое на строке в 32 000
знаков перебирает варианты секундами, останавливает потоки ответов, вход и проверку
сессий у всех — и вынос в пул потоков от этого не спасает. Поэтому у каждого разбора
текста, пришедшего от пользователя, из файла или от модели, здесь есть входы, подобранные
против него, и предел времени.
"""

import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest

from portal.core.settings import Settings
from portal.dialogs.context import Turn, data_block, fit_history
from portal.dialogs.export import xml_safe
from portal.dialogs.footnotes import cited_numbers
from portal.dialogs.generation import clean_title
from portal.dialogs.markdown import Heading, Paragraph, Table, parse_blocks, parse_spans
from portal.dialogs.service import export_file_name
from portal.files.names import display_file_name
from portal.kb.chunking import split_page
from portal.kb.context import knowledge_context
from portal.kb.ports import Source
from portal.llm.estimator import RatioTokenEstimator
from portal.tools.docparse import extracted_fields
from portal.tools.sql_check import find_dangers, question_dangers
from tests.test_sql_check import RULES

# Предел длины вопроса (`dialogs.message_max_chars`); ответ модели — того же порядка.
N = 32_000
# Линейный разбор такого входа занимает миллисекунды; квадратичный — секунды.
TIME_LIMIT_SECONDS = 0.5


def _fill(unit: str) -> str:
    return (unit * (N // len(unit) + 1))[:N]


# Находки ревью: заголовок из решёток и «разделитель таблицы» из пробелов.
HEADING_OF_HASHES = "# " + "#" * (N - 10) + "x"
TABLE_RULE_OF_SPACES = "a|b\n" + " " * (N - 10) + "x"

INPUTS = {
    "heading_of_hashes": HEADING_OF_HASHES,
    "table_rule_of_spaces": TABLE_RULE_OF_SPACES,
    "heading_hash_space": "# " + _fill("# "),
    "table_rule_cells": "a|b\n" + _fill("|-") + "x",
    "table_rule_dashes": "a|b\n" + "-" * N + "x",
    "table_rule_colons": "a|b\n" + ":" * N,
    "letters": "a" * N,
    "cyrillic": "я" * N,
    "letters_digits": _fill("a1"),
    "spaces": " " * N,
    "spaces_then_letter": " " * (N - 1) + "x",
    "newlines": "\n" * N,
    "newline_space": _fill("\n "),
    "newline_vertical_tabs": "\n" + "\x0b" * N,
    "stars": "*" * N,
    "star_letter": _fill("*a"),
    "bold_openings": _fill("**a"),
    "bold_spaces": _fill("** "),
    "underscores": "_" * N,
    "underscore_openings": _fill("__a "),
    "backticks": "`" * N,
    "backtick_letter": _fill("`a"),
    "backtick_runs_of_every_length": "".join("`" * k + "a" for k in range(1, 250)),
    "single_then_double_backticks": "`a" + _fill("``a"),
    "angle_brackets": "<" * N,
    "angle_space": _fill("< "),
    "closing_tag_starts": _fill("</ "),
    "closing_tag_spaces": _fill("< / "),
    "angle_then_spaces": "<" + " " * N,
    "pipes": "|" * N,
    "fences": _fill("```\n"),
    "tildes": _fill("~~~\n"),
    "list_items": _fill("- a\n"),
    "list_of_fence_like_lines": "- a\n" + _fill("- ```\n"),
    "list_of_indented_fences": "- a\n" + _fill("    ```\n"),
    "list_of_almost_fences": "1. a\n" + _fill("``\n"),
    "list_items_and_fences": _fill("1. a\n```sql\n"),
    "list_items_and_blocks": _fill("- a\n```sql\nb\n```\n"),
    "list_items_and_unclosed_tildes": _fill("- a\n~~~\n```\n"),
    "list_marker_then_spaces": "-" + " " * N,
    "numbered_items": _fill("1. a\n"),
    "footnote_openings": _fill("[1"),
    "footnotes": _fill("[1]"),
    "footnote_of_digits": "[" + "1" * N,
    "control_characters": "\x01" * N,
    "slashes": "/" * N,
    "backslashes": "\\" * N,
    "extensions": _fill(".pdf"),
    "sentences": _fill(". "),
    "quotes": "'" * N,
    "dollar_tags": _fill("$a$"),
    "comment_openings": _fill("/*"),
    "line_comments": _fill("--\n"),
    "parentheses": "(" * N,
    "semicolons": ";" * N,
}

_SOURCE_FIELDS: dict[str, Any] = {
    "n": 1,
    "document_id": uuid4(),
    "scope": "shared",
    "page": 1,
    "fragment_id": uuid4(),
}

PARSERS: dict[str, Callable[[str], object]] = {
    "markdown.parse_blocks": parse_blocks,
    "markdown.parse_spans": lambda text: [parse_spans(line) for line in text.split("\n")],
    "footnotes.cited_numbers": lambda text: cited_numbers(text, {1}),
    "context.data_block": lambda text: data_block("вложение", text, text),
    "kb.context.knowledge_context": lambda text: knowledge_context(
        [Source(document_title=text, quote=text, **_SOURCE_FIELDS)]
    ),
    # Страница документа базы знаний длиннее вопроса.
    "kb.chunking.split_page": lambda text: split_page(text * 10, 1200, 150),
    "files.names.display_file_name": display_file_name,
    "service.export_file_name": export_file_name,
    "generation.clean_title": clean_title,
    "export.xml_safe": xml_safe,
    "sql_check.find_dangers": lambda text: find_dangers([text], RULES),
    "sql_check.question_dangers": lambda text: question_dangers(text, RULES),
    "sql_check.question_dangers(select)": lambda text: question_dangers("SELECT " + text, RULES),
}


@pytest.mark.parametrize("parser", PARSERS)
def test_parsers_of_untrusted_text_are_linear(parser: str) -> None:
    parse = PARSERS[parser]
    for name, text in INPUTS.items():
        started = time.perf_counter()
        parse(text)
        elapsed = time.perf_counter() - started
        assert elapsed < TIME_LIMIT_SECONDS, f"{parser} на входе {name}: {elapsed:.2f} с"


def test_review_findings_are_parsed_as_before() -> None:
    assert parse_blocks(HEADING_OF_HASHES) == [Heading(1, "#" * (N - 10) + "x")]
    assert parse_blocks(TABLE_RULE_OF_SPACES) == [Paragraph("a|b\nx")]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("# Заголовок", Heading(1, "Заголовок")),
        ("## Заголовок ##", Heading(2, "Заголовок")),
        ("### Заголовок ###   ", Heading(3, "Заголовок")),
        ("# Заголовок#", Heading(1, "Заголовок")),
        ("#\tC# и F#", Heading(1, "C# и F")),
        ("# a # b", Heading(1, "a # b")),
        ("# ##", Heading(1, "")),
        ("   ###### шестой уровень", Heading(6, "шестой уровень")),
        ("####### семь решёток", Paragraph("####### семь решёток")),
        ("#без пробела", Paragraph("#без пробела")),
    ],
)
def test_heading_text_drops_closing_hashes(line: str, expected: Heading | Paragraph) -> None:
    assert parse_blocks(line) == [expected]


@pytest.mark.parametrize(
    ("rule", "is_table"),
    [
        ("|---|---|", True),
        ("---|---", True),
        (" | :--- | ---: | :-: | ", True),
        ("-", True),
        ("|-|", True),
        ("||", False),
        ("|---||---|", False),
        ("| --- | текст |", False),
        ("|:|", False),
        ("| - - |", False),
        ("", False),
        ("|", False),
    ],
)
def test_table_rule_is_recognised_by_cells(rule: str, is_table: bool) -> None:
    blocks = parse_blocks(f"a|b\n{rule}\n1|2")
    assert isinstance(blocks[0], Table) is is_table


def test_history_is_fitted_in_one_pass() -> None:
    """Длинный диалог, который почти весь не помещается: суммы не пересчитываются заново."""
    estimator = RatioTokenEstimator(chars_per_token=3.0, tokens_per_image=1000)
    history = [Turn("user" if index % 2 == 0 else "assistant", "x" * N) for index in range(20_000)]
    started = time.perf_counter()
    dropped = fit_history("система", history, Turn("user", "вопрос"), 25_000, estimator)
    assert time.perf_counter() - started < TIME_LIMIT_SECONDS
    assert dropped == 19_998


def test_deeply_nested_model_answer_is_not_a_table(settings: Settings) -> None:
    template = settings.docparse.templates[0]
    for answer in ("[" * 100_000, '{"a":' * 100_000):
        assert extracted_fields(template, answer, 10) is None
