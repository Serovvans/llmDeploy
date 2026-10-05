"""Контрольный набор и оценка поиска (docs/portal-design.md §5.2)."""

from collections.abc import AsyncIterator

import pytest
from qdrant_client import AsyncQdrantClient

from portal.core.settings import Settings
from portal.kb import evalset
from portal.kb.evaluation import EvalDocument, EvalQuestion, evaluate, format_report
from portal.kb.qdrant import QdrantVectorIndex
from tests.kb_support import WordHashEmbedder

pytestmark = pytest.mark.anyio


@pytest.fixture
async def index(settings: Settings) -> AsyncIterator[QdrantVectorIndex]:
    client = AsyncQdrantClient(location=":memory:")
    built = QdrantVectorIndex(client, settings.kb, "kb_fragments_eval")
    await built.ensure_collection()
    yield built
    await client.close()


def test_evalset_has_russian_legal_and_technical_questions_with_pages() -> None:
    documents, questions = evalset.load()
    assert 20 <= len(questions) <= 30
    kinds = [question.kind for question in questions]
    assert kinds.count("exact") >= 8 and kinds.count("semantic") >= 8
    assert {question.document for question in questions} <= {document.id for document in documents}
    assert all(question.pages and question.text.strip() for question in questions)
    cyrillic = sum(
        1
        for document in documents
        for page in document.pages
        for char in page
        if "а" <= char <= "я"
    )
    assert cyrillic > 5000


async def test_evaluation_counts_hits_by_document_and_page(
    index: QdrantVectorIndex, settings: Settings
) -> None:
    documents = [
        EvalDocument("lease", "Договор.pdf", ("Предмет договора аренды", "Срок аренды 49 лет")),
        EvalDocument("guide", "Памятка.md", ("Индекс GiST ускоряет поиск",)),
    ]
    questions = [
        EvalQuestion("срок аренды 49 лет", "lease", (2,), "exact"),
        EvalQuestion("индекс GiST", "guide", (1,), "exact"),
        EvalQuestion("индекс GiST поиск", "lease", (1,), "semantic"),  # ответ не там
    ]
    report = await evaluate(documents, questions, WordHashEmbedder(), index, settings.kb)
    assert report.fragments == 3
    assert report.total.questions == 3
    assert report.total.hits[1] == pytest.approx(2 / 3)
    assert report.by_kind["exact"].hits[1] == 1.0
    assert report.by_kind["semantic"].hits == {1: 0.0, 3: 1.0, 8: 1.0}
    assert report.by_kind["semantic"].mrr == pytest.approx(1 / 3)
    assert report.misses == ()


async def test_shipped_evalset_runs_and_exact_questions_are_found(
    index: QdrantVectorIndex, settings: Settings
) -> None:
    """На хешах слов вместо настоящих эмбеддингов работает только лексика.

    Вопросы на точное совпадение обязаны находиться уже так; качество на вопросах «по
    смыслу» зависит от модели эмбеддингов и проверяется на ВМ.
    """
    documents, questions = evalset.load()
    report = await evaluate(documents, questions, WordHashEmbedder(), index, settings.kb)
    assert report.total.questions == len(questions)
    exact = report.by_kind["exact"]
    assert exact.hits[settings.kb.search.top_k] == 1.0
    # Пороги с запасом от измеренного (hit@1 0.94, hit@3 0.94, MRR 0.95): тест ловит
    # заметное ухудшение правил, а не правку одного вопроса набора.
    assert exact.hits[1] >= 0.85 and exact.hits[3] >= 0.9 and exact.mrr >= 0.9
    assert report.total.hits[1] >= 0.65 and report.total.hits[3] >= 0.75
    assert report.total.mrr >= 0.7
    text = format_report(report)
    assert "все вопросы" in text and "точное совпадение" in text and "hit@8" in text


MANDATORY = (
    "Какой участок принадлежит Соколовой Марине Викторовне?",  # фамилия в косвенном падеже
    "Чем владеет Соколов Андрей Петрович?",  # «Соколов» рядом с «Соколова»
    "Что зарегистрировано за Семеновой Ольгой Игоревной?",  # «ё» и падеж
    "Какой адрес у участка 77:01:0004012:346?",  # кадастровый номер среди соседних
    "Кто правообладатель участка 77:01:0004012:345?",  # целый номер против повторов частей
    "Кто арендатор по договору 14 - A?",  # латинская буква и пробелы в номере
    "Что сдаётся в аренду по договору 14-Б?",
)


async def test_mandatory_questions_put_expected_document_first_among_similar(
    index: QdrantVectorIndex, settings: Settings
) -> None:
    """Обязательные случаи §10.5: нужный документ первый среди однотипных, уже на лексике."""
    documents, questions = evalset.load()
    mandatory = [question for question in questions if question.text in MANDATORY]
    assert len(mandatory) == len(MANDATORY)
    assert sum(1 for document in documents if document.title.startswith("Выписка ЕГРН")) >= 4
    report = await evaluate(documents, mandatory, WordHashEmbedder(), index, settings.kb)
    assert report.total.hits[1] == 1.0
