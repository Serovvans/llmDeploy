"""Разбиение страницы на фрагменты (docs/portal-api.md §8.3, §9.13)."""

from itertools import pairwise

import pytest

from portal.kb.chunking import split_page

PARAGRAPHS = "\n\n".join(
    f"Пункт {number}. " + "Арендатор обязан вносить плату в срок. " * 6 for number in range(1, 9)
)


def test_short_page_is_one_fragment_without_edge_whitespace() -> None:
    text = "  \n Договор аренды № 14-А от 12 марта 2024 года.\n\n "
    spans = split_page(text, max_chars=200, overlap_chars=20)
    assert [text[start:end] for start, end in spans] == [
        "Договор аренды № 14-А от 12 марта 2024 года."
    ]


@pytest.mark.parametrize("text", ["", "   \n\t  "])
def test_empty_page_gives_no_fragments(text: str) -> None:
    assert split_page(text, max_chars=100, overlap_chars=10) == []


def test_fragments_respect_limit_and_cover_the_whole_page() -> None:
    spans = split_page(PARAGRAPHS, max_chars=300, overlap_chars=40)
    assert len(spans) > 3
    covered: set[int] = set()
    for start, end in spans:
        fragment = PARAGRAPHS[start:end]
        assert 0 < len(fragment) <= 300
        assert fragment == fragment.strip()
        covered.update(range(start, end))
    visible = {index for index, char in enumerate(PARAGRAPHS) if not char.isspace()}
    assert visible <= covered


def test_fragments_go_in_order_and_overlap_without_cutting_words() -> None:
    spans = split_page(PARAGRAPHS, max_chars=300, overlap_chars=40)
    for (start, end), (next_start, next_end) in pairwise(spans):
        assert start < next_start <= end < next_end
        assert PARAGRAPHS[next_start - 1].isspace()


def test_cut_prefers_paragraph_boundary() -> None:
    first = "Первый абзац договора. " * 8
    text = f"{first.strip()}\n\nВторой абзац начинается здесь и продолжается дальше. " * 2
    start, end = split_page(text, max_chars=len(first) + 40, overlap_chars=0)[0]
    assert text[start:end] == first.strip()


def test_text_without_spaces_is_cut_hard_and_terminates() -> None:
    text = "я" * 1050
    spans = split_page(text, max_chars=100, overlap_chars=20)
    assert spans[0] == (0, 100)
    assert spans[-1][1] == len(text)
    assert all(end - start <= 100 for start, end in spans)


def test_offsets_are_code_points_not_bytes() -> None:
    text = "Участок 😀 площадью 1 250 кв. м. " * 20
    for start, end in split_page(text, max_chars=120, overlap_chars=10):
        assert text[start:end] == text[start:end].strip() != ""
