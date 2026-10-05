"""Русский текстовый слой PDF: извлечение, фрагменты, точный поиск (ПД §5.2).

Документы заказчика русские: кадастровые номера, фамилии, юридические тексты. Образец
PDF строится в памяти (`tests.kb_support.cyrillic_pdf`) и проходит весь рабочий путь:
загрузка → читатель на PDFium → фрагменты → Qdrant → порт `KnowledgeBase`.
"""

from uuid import UUID

import pytest
from qdrant_client import models

from portal.kb.lexical import sparse_vector
from portal.kb.qdrant import access_filter
from tests.kb_support import KbBench, added, cyrillic_pdf

pytestmark = pytest.mark.anyio

EXTRACT = [
    "ВЫПИСКА ИЗ ЕДИНОГО ГОСУДАРСТВЕННОГО РЕЕСТРА НЕДВИЖИМОСТИ",
    "Кадастровый номер: 77:01:0004012:345",
    "Адрес: г. Москва, улица Большая Ордынка, владение 40",
    "Площадь: 2 318 +/- 17 кв. м, доля в праве 1/2",
]
RIGHTS = [
    "Правообладатель: Соколова Марина Викторовна",
    "Собственность, № 77:01:0004012:345-77/051/2019-3 от 14.02.2019",
    "Кадастровый инженер Семёнов Пётр Алексеевич, аттестат № 77-15-482",
    "Основание: постановление администрации № 123/2024-ПП",
]
LONG = [f"Пункт {n}. Арендатор обязан вносить арендную плату в срок." for n in range(1, 50)]


async def _lexical_only(bench: KbBench, query: str, user: UUID) -> list[tuple[int, str]]:
    """Найденное одной лексической ветвью — без слияния с векторной, но с фильтром доступа."""
    vector = sparse_vector(query)
    result = await bench.qdrant.query_points(
        bench.settings.qdrant.collection,
        query=models.SparseVector(indices=list(vector.indices), values=list(vector.values)),
        using="lexical",
        query_filter=access_filter(user, "shared"),
        limit=3,
    )
    rows = await bench.portal.rows(
        "SELECT f.id, f.page_number, substr(p.text, f.start_offset + 1, "
        "f.end_offset - f.start_offset) AS text FROM kb_fragments f JOIN kb_pages p "
        "ON p.document_id = f.document_id AND p.number = f.page_number"
    )
    by_id = {str(row.id): (row.page_number, row.text) for row in rows}
    return [by_id[str(point.id)] for point in result.points]


async def test_russian_text_layer_is_extracted_split_and_found_exactly(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    user = await portal.user_id("ivanov")
    await added(client, "Выписка ЕГРН.pdf", cyrillic_pdf([EXTRACT, RIGHTS, LONG]))
    # Соседние номера и другая фамилия: точное совпадение должно отличить нужный документ.
    for number, surname in ((344, "Соколов"), (346, "Соколовская"), (3450, "Сокольникова")):
        noise = [f"Кадастровый номер: 77:01:0004012:{number}", f"Правообладатель: {surname} А. В."]
        await added(client, f"Выписка {number}.pdf", cyrillic_pdf([noise]))
    await bench.drain()

    statuses = await portal.rows("SELECT status, error_code FROM kb_documents")
    assert {(row.status, row.error_code) for row in statuses} == {("ready", None)}
    pages = await portal.rows(
        "SELECT p.number, p.text, p.recognized FROM kb_pages p JOIN kb_documents d "
        "ON d.id = p.document_id WHERE d.title = 'Выписка ЕГРН.pdf' ORDER BY p.number"
    )
    # Текстовый слой прочитан как есть: распознавание моделью не понадобилось.
    assert [page.recognized for page in pages] == [False, False, False]
    assert bench.recognizer.calls == []
    assert pages[0].text.splitlines() == EXTRACT
    assert pages[1].text.splitlines() == RIGHTS
    assert "Семёнов Пётр" in pages[1].text  # «ё» в тексте страницы сохраняется
    long_fragments = await portal.rows(
        "SELECT f.start_offset, f.end_offset FROM kb_fragments f JOIN kb_documents d "
        "ON d.id = f.document_id WHERE d.title = 'Выписка ЕГРН.pdf' AND f.page_number = 3"
    )
    assert len(long_fragments) > 1
    for fragment in long_fragments:
        text = pages[2].text[fragment.start_offset : fragment.end_offset]
        assert 0 < len(text) <= bench.settings.chunking.max_chars
        assert text[0] != " " and "Арендатор" in text

    # Лексическая ветвь сама по себе: кадастровый номер, фамилия, номера с дробью.
    assert (await _lexical_only(bench, "77:01:0004012:345", user))[0] == (1, pages[0].text)
    for query in (
        "Соколова",
        "СОКОЛОВА МАРИНА",
        "Семенов",  # в документе «Семёнов»
        "Семёнов",
        "123/2024-ПП",
        "123/2024-пп",
        "77-15-482",
        "77:01:0004012:345-77/051/2019-3",
    ):
        found = await _lexical_only(bench, query, user)
        assert found and found[0] == (2, pages[1].text), query
    # Склонённая фамилия находит именительный падеж — по общей основе (§10.5).
    for query in ("Соколовой Марине", "Соколову Марину", "Семенову", "Семёновым"):
        found = await _lexical_only(bench, query, user)
        assert found and found[0] == (2, pages[1].text), query
    # Одна склонённая фамилия без имени: основа у «Соколова» и «Соколов» общая, род по
    # ней не различить — находятся оба документа и только они (не «Соколовская»).
    alone = await _lexical_only(bench, "Соколовой", user)
    assert {text.splitlines()[-1] if page == 1 else "Соколова" for page, text in alone} == {
        "Правообладатель: Соколов А. В.",
        "Соколова",
    }

    # Весь путь через порт: вопрос словами, ответ — нужный документ и страница.
    by_number = await bench.knowledge.retrieve("Чей участок 77:01:0004012:345?", user, "shared")
    # Обе страницы выписки содержат этот номер (вторая — в номере записи о праве), и обе
    # идут раньше выписок с соседними номерами.
    assert {(source.document_title, source.page) for source in by_number.sources[:2]} == {
        ("Выписка ЕГРН.pdf", 1),
        ("Выписка ЕГРН.pdf", 2),
    }
    # Вопрос со склонённой фамилией: нужная страница — первый источник, раньше выписок
    # с фамилиями «Соколов», «Соколовская», «Сокольникова».
    for question in ("Что принадлежит Соколовой Марине?", "Соколова Марина Викторовна"):
        by_name = await bench.knowledge.retrieve(question, user, "shared")
        first = by_name.sources[0]
        assert (first.n, first.document_title, first.page) == (1, "Выписка ЕГРН.pdf", 2), question
    assert "] Выписка ЕГРН.pdf, стр. 1\n<фрагмент>\n" in by_number.context


async def test_pdf_without_unicode_table_goes_to_recognition_not_indexed_as_garbage(
    bench: KbBench,
) -> None:
    """Слой без таблицы соответствия шрифта негоден: страница распознаётся как скан (§1.5)."""
    portal = bench.portal
    client = await portal.employee()
    await added(client, "нормальный.pdf", cyrillic_pdf([RIGHTS]))
    await added(client, "без таблицы.pdf", cyrillic_pdf([RIGHTS], to_unicode=False))
    await bench.drain()
    pages = {
        row.title: (row.text, row.recognized)
        for row in await portal.rows(
            "SELECT d.title, p.text, p.recognized FROM kb_pages p "
            "JOIN kb_documents d ON d.id = p.document_id"
        )
    }
    assert pages["нормальный.pdf"] == ("\r\n".join(RIGHTS), False)  # порог 0.8 его не задел
    text, recognized = pages["без таблицы.pdf"]
    assert recognized is True and text.startswith("Распознанный текст скана")
    assert bench.recognizer.calls == [1]
    row = (
        await portal.rows("SELECT recognizing FROM kb_documents WHERE title = 'без таблицы.pdf'")
    )[0]
    assert row.recognizing is True
