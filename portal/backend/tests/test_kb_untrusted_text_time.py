"""Разборы базы знаний линейны на недоверенном тексте: вопрос, название файла, документ.

Лексический вектор вопроса считается в цикле событий единственного процесса
`portal-api`, а `re` не отпускает GIL: квадратичное выражение на вопросе предельной длины
останавливало бы потоки ответов у всех. На каждый разбор — входы, подобранные против
него, и предел времени (образец — `tests/test_untrusted_text_time.py`).
"""

import random
import re
import time
from collections.abc import Callable
from uuid import uuid4

import pytest

from portal.core.settings import Settings
from portal.kb import lexical
from portal.kb.chunking import split_page
from portal.kb.context import fit_sources, knowledge_context
from portal.kb.domain import FoundFragment
from portal.kb.lexical import sparse_vector, tokenize
from portal.kb.ports import Source
from portal.llm.estimator import RatioTokenEstimator

# Вопрос обрезается до `kb.embeddings.max_input_chars`; запас — до предела сообщения.
QUESTION = 8_000
MESSAGE = 32_000
# Линейный разбор таких входов — миллисекунды; квадратичный — секунды.
TIME_LIMIT_SECONDS = 0.5


def _fill(unit: str, length: int) -> str:
    return (unit * (length // len(unit) + 1))[:length]


def _elapsed(call: Callable[[], object]) -> float:
    started = time.perf_counter()
    call()
    return time.perf_counter() - started


# Отрезки без знака номера (находка), знаки с пробелами и без, слова вперемешку, основы.
TOKEN_UNITS = [
    "я", "7", "a", "ё", "а1", "aя", "ться", "_", "-", "/", ":", ".", " ", "\xa0", "—",
    "я-", "7 - ", "7-а ", "7:", "я7-", "7\xa0-\xa0", "я ", "a ", "соколовой ", "ейшими ",
    "7" + " " * 40 + "/" + " " * 40, "7" + " " * 40, "- ", " -", "я/", "/7", "а.б", "a-1 ",
]  # fmt: skip


@pytest.mark.parametrize("unit", TOKEN_UNITS)
@pytest.mark.parametrize("length", [QUESTION, MESSAGE])
def test_lexical_vector_of_question_is_linear(unit: str, length: int) -> None:
    text = _fill(unit, length)
    assert _elapsed(lambda: sparse_vector(text)) < TIME_LIMIT_SECONDS


def test_many_distinct_words_with_cold_stem_cache_are_fast() -> None:
    words = [f"{chr(1072 + i % 32)}{chr(1072 + i // 32 % 32)}{i}овыми" for i in range(900)]
    text = " ".join(word.translate(str.maketrans("0123456789", "абвгдежзик")) for word in words)
    assert _elapsed(lambda: sparse_vector(text[:QUESTION])) < TIME_LIMIT_SECONDS


def test_long_pseudo_words_do_not_grow_stem_cache() -> None:
    """Отрезки длиннее настоящих слов в кеш основ не кладутся: память им не раздуть."""
    before = lexical._cached_stem.cache_info().currsize
    for index in range(50):
        tokenize("я" * 7_000 + chr(1072 + index % 32) * 900)
    assert lexical._cached_stem.cache_info().currsize == before
    assert tokenize("я" * 60)[0] == "я" * 60  # основа длинного отрезка всё равно считается


def test_spaced_sign_rule_gives_the_same_result_as_before_the_fix() -> None:
    """Просмотр назад в начале выражения меняет только время: векторы прежние."""
    previous = re.compile(r"([^\W_]+)[ \t\xa0]*([-/])[ \t\xa0]*(?=([^\W_]+))")
    generator = random.Random(20261005)
    alphabet = "аб1 9-/\t\xa0x_.:,\n"
    for _ in range(20_000):
        text = "".join(generator.choice(alphabet) for _ in range(generator.randint(0, 24)))
        assert lexical._SPACED_SIGN.sub(lexical._join_spaced_number, text) == previous.sub(
            lexical._join_spaced_number, text
        ), repr(text)


# Документ без страниц доходит до `kb.indexing.document_max_chars` символов одной «страницей».
PAGE_UNITS = ["я", " ", "\n", "\n ", "я\n\n", "я. ", ". ", "\n\t\t\tя", "я ", "!\n", "…"]


@pytest.mark.parametrize("unit", PAGE_UNITS)
def test_splitting_page_of_document_limit_is_linear(unit: str, settings: Settings) -> None:
    length = settings.kb.indexing.document_max_chars
    text = _fill(unit, length)
    chunking = settings.kb.chunking
    assert _elapsed(lambda: split_page(text, chunking.max_chars, chunking.overlap_chars)) < 3.0


def test_page_of_spaces_ending_with_letter_is_linear(settings: Settings) -> None:
    text = " " * (settings.kb.indexing.document_max_chars - 1) + "x"
    assert _elapsed(lambda: split_page(text, 1200, 150)) < 3.0


# Название файла (до 255 знаков) и текст фрагмента (до `kb.chunking.max_chars`).
BLOCK_UNITS = ["<", "< ", "</", "</фрагмент", "<  /  ", "[", "]", "[1] ", "\n", "</база_знаний>"]


@pytest.mark.parametrize("unit", BLOCK_UNITS)
def test_context_block_is_linear_in_title_and_fragment(unit: str, settings: Settings) -> None:
    text = _fill(unit, MESSAGE)  # с запасом против предела фрагмента
    sources = [
        Source(n, uuid4(), text[:255], "shared", 1, uuid4(), text)
        for n in range(1, settings.kb.search.top_k + 1)
    ]
    assert _elapsed(lambda: knowledge_context(sources)) < TIME_LIMIT_SECONDS
    found = [FoundFragment(uuid4(), uuid4(), text[:255], "shared", 1, text) for _ in sources]
    estimator = RatioTokenEstimator(2.0, 3200)
    assert _elapsed(lambda: fit_sources(found, "правила", 10**9, estimator)) < TIME_LIMIT_SECONDS
