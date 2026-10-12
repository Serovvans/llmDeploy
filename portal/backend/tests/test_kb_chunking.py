"""Разбиение на фрагменты и текст для поиска (docs/portal-api.md §8.3, §9.13, §10.1)."""

from itertools import pairwise

import pytest

from portal.core.settings import Settings
from portal.kb.chunking import split_document, split_page
from portal.kb.search_text import search_text

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


# --- структура документа ---

CONTRACT = (
    "ДОГОВОР № 14-А\nаренды земельного участка\n\n"
    "1. Предмет договора\n1.1. Арендодатель предоставляет участок.\n"
    "1.2. Участок передаётся по акту.\n\n"
    "2. Срок договора\n2.1. Участок предоставляется на 49 лет.\n"
)


def _split(
    pages: list[tuple[int, str]], settings: Settings, **limits: int
) -> list[tuple[int, str, str | None]]:
    chunking = settings.kb.chunking
    spans = split_document(
        pages,
        chunking.heading_patterns,
        limits.get("max_chars", chunking.max_chars),
        limits.get("overlap_chars", chunking.overlap_chars),
        limits.get("section_max_chars", chunking.section_max_chars),
    )
    texts = dict(pages)
    return [(s.page_number, texts[s.page_number][s.start : s.end], s.section) for s in spans]


def test_fragment_never_crosses_a_section_boundary(settings: Settings) -> None:
    """Короткая страница без структуры — один фрагмент; заголовки делят её на разделы."""
    fragments = _split([(1, CONTRACT)], settings)
    assert [section for _, _, section in fragments] == [
        None,
        "1. Предмет договора",
        "2. Срок договора",
    ]
    # Подпункты остаются в своём разделе, заголовок — в первом фрагменте раздела.
    assert fragments[1][1].startswith("1. Предмет договора\n1.1.") and "1.2." in fragments[1][1]
    assert "2. Срок" not in fragments[1][1]
    assert fragments[2][1] == "2. Срок договора\n2.1. Участок предоставляется на 49 лет."


def test_section_continues_on_next_pages_until_new_heading(settings: Settings) -> None:
    pages = [
        (1, "# Системы координат\nГеометрии хранятся в EPSG:4326."),
        (2, "Считать площадь по таким координатам нельзя.\nСтатья 5. Индексы\nНужен GiST."),
        (3, "После загрузки выполните ANALYZE."),
    ]
    assert [(page, section) for page, _, section in _split(pages, settings)] == [
        (1, "# Системы координат"),
        (2, "# Системы координат"),
        (2, "Статья 5. Индексы"),
        (3, "Статья 5. Индексы"),
    ]


def test_numbered_list_inside_a_section_does_not_become_a_section(settings: Settings) -> None:
    """Пункт перечня — предложение, а не заголовок: раздел после него прежний."""
    pages = [
        (
            1,
            "2. Обязанности сторон\nАрендатор обязан:\n"
            "1. Вносить плату в срок.\n2. Не допускать загрязнения.\n"
            "3. Соблюдать требования, в том числе:\n4. Освободить участок по окончании срока;",
        ),
        (2, "Арендодатель вправе расторгнуть договор при нарушении условий."),
        (3, "3. Ответственность сторон\nЗа просрочку начисляется пеня."),
    ]
    assert [(page, section) for page, _, section in _split(pages, settings)] == [
        (1, "2. Обязанности сторон"),
        (2, "2. Обязанности сторон"),
        (3, "3. Ответственность сторон"),
    ]


def test_numbered_lines_in_a_row_are_a_list_not_sections(settings: Settings) -> None:
    """Перечень без знаков в конце пунктов остаётся целым в своём разделе."""
    page = (
        "2. Срок договора\n2.1. Договор действует 5 лет.\nПеречень документов\n"
        "1. Заявление\n\n2. Копия паспорта\n3. Выписка из ЕГРН\n"
        "Статья 5. Порядок подачи\nДокументы подаются лично."
    )
    fragments = _split([(1, page)], settings)
    assert [section for _, _, section in fragments] == [
        "2. Срок договора",
        "Статья 5. Порядок подачи",
    ]
    assert fragments[0][1].endswith(
        "Перечень документов\n1. Заявление\n\n2. Копия паспорта\n3. Выписка из ЕГРН"
    )
    # Одиночная нумерованная строка и заголовки Markdown подряд — по-прежнему заголовки.
    headings = "# Руководство\n## Установка\n1. Требования\nНужен PostGIS."
    assert [section for _, _, section in _split([(1, headings)], settings)] == [
        "# Руководство",
        "## Установка",
        "1. Требования",
    ]


def test_long_section_is_split_as_before_and_keeps_its_name(settings: Settings) -> None:
    body = "Арендатор обязан вносить плату в срок. " * 30
    fragments = _split(
        [(1, f"3. Арендная плата\n{body}")], settings, max_chars=300, overlap_chars=40
    )
    assert len(fragments) > 3
    assert all(len(text) <= 300 for _, text, _ in fragments)
    assert {section for _, _, section in fragments} == {"3. Арендная плата"}


def test_text_without_headings_is_split_exactly_as_a_plain_page(settings: Settings) -> None:
    """Сканы и расшифровки без структуры режутся прежним правилом."""
    chunking = settings.kb.chunking
    spans = split_document([(7, PARAGRAPHS)], chunking.heading_patterns, 400, 60, 200)
    assert [(span.start, span.end) for span in spans] == split_page(PARAGRAPHS, 400, 60)
    assert all(span.section is None and span.page_number == 7 for span in spans)


def test_search_text_parts_order_and_longest_case_fits_the_startup_check(
    settings: Settings,
) -> None:
    chunking = settings.kb.chunking
    assert (
        search_text("Договор\n№ 14-А.pdf", "2. Срок", "О сроке аренды.", "на 49 лет", chunking)
        == "Документ: Договор № 14-А.pdf\nРаздел: 2. Срок\nО сроке аренды.\n\nна 49 лет"
    )
    # Отсутствующая часть пропускается: так выглядит фрагмент, созданный до 0.15.
    assert search_text("Акт.pdf", None, None, "текст", chunking) == "Документ: Акт.pdf\n\nтекст"
    # Проверка при старте (§13.6) считает ровно эту наибольшую длину.
    longest = search_text(
        "н" * 999,
        "р" * 999,
        "п" * settings.kb.context.max_chars,
        "т" * (chunking.max_chars + 500),  # описание картинки длиннее фрагмента обрезается
        chunking,
    )
    assert len(longest) == settings.kb.search_text_max_chars
