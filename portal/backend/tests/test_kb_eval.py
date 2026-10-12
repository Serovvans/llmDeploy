"""Контрольный набор и оценка поиска (docs/portal-design.md §5.2)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from qdrant_client import AsyncQdrantClient

from portal.core.settings import KbSettings, Settings
from portal.kb import evalset
from portal.kb.evaluation import (
    EvalDocument,
    EvalPorts,
    EvalQuestion,
    evaluate,
    format_report,
    idle_features,
)
from portal.kb.explaining import FragmentExplainer
from portal.kb.ports import DialogTurn, RerankUnavailableError
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.rewrite import QueryRewriter
from portal.llm.estimator import RatioTokenEstimator
from tests.kb_support import ProtocolModel, WordHashEmbedder, WordOverlapReranker

pytestmark = pytest.mark.anyio


@pytest.fixture
async def qdrant() -> AsyncIterator[AsyncQdrantClient]:
    client = AsyncQdrantClient(location=":memory:")
    yield client
    await client.close()


async def _ports(qdrant: AsyncQdrantClient, kb: KbSettings) -> EvalPorts:
    """Порты прогона на подменах стенда: хеши слов, ответы по протоколам заглушки."""
    index = QdrantVectorIndex(qdrant, kb, "kb_fragments_eval")
    await index.drop_collection()
    await index.ensure_collection()
    model = ProtocolModel()
    return EvalPorts(
        embedder=WordHashEmbedder(),
        index=index,
        rewriter=QueryRewriter(model, kb.rewrite),
        explainer=FragmentExplainer(
            model, RatioTokenEstimator(2.0, 3200), kb.context, asyncio.Semaphore(2)
        ),
        reranker=WordOverlapReranker(),
    )


async def _reset(ports: EvalPorts) -> EvalPorts:
    """Те же порты с пустой коллекцией — для следующего прогона."""
    await ports.index.drop_collection()
    await ports.index.ensure_collection()
    return ports


def _without(kb: KbSettings, *features: str) -> KbSettings:
    return kb.model_copy(
        update={name: getattr(kb, name).model_copy(update={"enabled": False}) for name in features}
    )


def test_evalset_has_russian_legal_and_technical_questions_with_pages() -> None:
    documents, questions = evalset.load()
    assert len(questions) >= 40
    kinds = [question.kind for question in questions]
    assert kinds.count("exact") >= 8 and kinds.count("semantic") >= 8
    # Уточняющие вопросы — с историей диалога, остальные — без неё.
    assert kinds.count("followup") >= 8
    assert all(bool(question.history) == (question.kind == "followup") for question in questions)
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


def test_evalset_from_directory_is_checked_like_the_shipped_one(tmp_path: Path) -> None:
    """Набор на документах заказчика лежит в каталоге на ВМ — в том же формате."""
    (tmp_path / "corpus.yaml").write_text(
        "documents:\n  - {id: act, title: Акт.pdf, pages: [первая страница, вторая страница]}\n",
        encoding="utf-8",
    )
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n  - {text: что на второй, document: act, pages: [2], kind: exact}\n",
        encoding="utf-8",
    )
    documents, asked = evalset.load(tmp_path)
    assert [document.title for document in documents] == ["Акт.pdf"]
    assert [(question.text, question.pages) for question in asked] == [("что на второй", (2,))]
    questions.write_text(
        "questions:\n  - {text: мимо, document: act, pages: [3], kind: exact}\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="несуществующую страницу"):
        evalset.load(tmp_path)
    # Опечатка в виде вопроса или в роли не должна молча выключать вопрос из замера.
    for item, reason in (
        ("{text: вид, document: act, pages: [1], kind: exakt}", "вид вопроса"),
        (
            "{text: роль, document: act, pages: [1], kind: followup,"
            " history: [{role: asistant, text: ответ}]}",
            "роль в истории",
        ),
    ):
        questions.write_text(f"questions:\n  - {item}\n", encoding="utf-8")
        with pytest.raises(ValueError, match=reason):
            evalset.load(tmp_path)
    # Пустой список вопросов дал бы деление на ноль уже после индексации набора.
    questions.write_text("questions: []\n", encoding="utf-8")
    with pytest.raises(ValueError, match="нет документов или вопросов"):
        evalset.load(tmp_path)
    questions.write_text(
        "questions:\n  - {text: число, document: act, pages: [1], kind: exact}\n", encoding="utf-8"
    )
    for corpus, reason in (
        ("documents: []\n", "нет документов или вопросов"),
        # Страница числом, а не строкой: YAML читает её без кавычек как число.
        ("documents:\n  - {id: act, title: Акт.pdf, pages: [123]}\n", "не строка"),
    ):
        (tmp_path / "corpus.yaml").write_text(corpus, encoding="utf-8")
        with pytest.raises(ValueError, match=reason):
            evalset.load(tmp_path)


async def test_evaluation_reports_each_stage_separately(
    qdrant: AsyncQdrantClient, settings: Settings
) -> None:
    """Полнота — среди кандидатов до реранкера; попадания, MRR и nDCG — по итоговой выдаче."""
    documents = [
        EvalDocument("lease", "Договор.pdf", ("Предмет договора аренды", "Срок аренды 49 лет")),
        EvalDocument("guide", "Памятка.md", ("Индекс GiST ускоряет поиск",)),
    ]
    history = (DialogTurn("user", "срок аренды"), DialogTurn("assistant", "Нашёл [1]."))
    questions = [
        EvalQuestion("срок аренды 49 лет", "lease", (2,), "exact"),
        EvalQuestion("индекс GiST", "guide", (1,), "exact"),
        EvalQuestion("индекс GiST поиск", "lease", (1,), "semantic"),  # ответ не там
        EvalQuestion("на сколько лет?", "lease", (2,), "followup", history),
    ]
    kb = _without(settings.kb, "context")
    report = await evaluate(documents, questions, await _ports(qdrant, kb), kb)
    assert (report.fragments, report.candidates) == (3, kb.rerank.candidates)
    assert report.enabled == ("rewrite", "rerank")
    assert report.total.questions == 4 and report.total.recall == 1.0
    assert report.by_kind["exact"].hits[1] == 1.0 and report.by_kind["exact"].ndcg == 1.0
    # Ответ среди кандидатов есть, но не первый: теряет его не поиск кандидатов, а порядок.
    semantic = report.by_kind["semantic"]
    assert semantic.recall == 1.0 and semantic.hits[1] == 0.0 and 0 < semantic.ndcg < 1
    assert semantic.mrr in (pytest.approx(1 / 2), pytest.approx(1 / 3))
    assert report.by_kind["followup"].hits[1] == 1.0
    assert report.lost_in_candidates == report.lost_in_ranking == ()

    # Без переписывания уточняющий вопрос ищется по своим словам — замер «до».
    before = _without(kb, "rewrite", "rerank")
    plain = await evaluate(documents, questions, await _ports(qdrant, before), before)
    assert plain.enabled == () and plain.candidates == kb.search.top_k
    text = format_report(plain)
    assert "Включено: ничего" in text and f"recall@{kb.search.top_k}" in text
    assert "уточняющие" in text and "nDCG@8" in text and "MRR" in text


async def test_silent_fallbacks_of_enabled_improvements_are_reported(
    qdrant: AsyncQdrantClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Реранкер недоступен, переписывание падает: замер не выдаёт себя за замер «с ними»."""
    documents = [
        EvalDocument("lease", "Договор.pdf", ("Предмет договора аренды", "Срок аренды 49 лет")),
        # Кандидатов больше, чем гарантированных лексических мест: реранкеру есть что упорядочить.
        EvalDocument("guide", "Памятка.md", tuple(f"Срок хранения, раздел {n}" for n in range(6))),
    ]
    history = (DialogTurn("user", "срок аренды"), DialogTurn("assistant", "Нашёл [1]."))
    questions = [
        EvalQuestion("срок аренды 49 лет", "lease", (2,), "exact"),
        EvalQuestion("на сколько лет?", "lease", (2,), "followup", history),
    ]
    kb = _without(settings.kb, "context")
    ports = await _ports(qdrant, kb)
    working = await evaluate(documents, questions, ports, kb)
    assert working.fallbacks == {"rewrite": (0, 1), "rerank": (0, 2)}
    assert idle_features(working) == () and "не ответил" not in format_report(working)

    async def unavailable(*_: object) -> list[float]:
        raise RerankUnavailableError

    async def failing(*_: object) -> str:
        raise TimeoutError

    monkeypatch.setattr(ports.reranker, "scores", unavailable)
    monkeypatch.setattr(ports.rewriter._model, "complete", failing)
    broken = await evaluate(documents, questions, await _reset(ports), kb)
    assert broken.enabled == ("rewrite", "rerank")
    assert broken.fallbacks == {"rewrite": (1, 1), "rerank": (2, 2)}
    assert idle_features(broken) == ("rewrite", "rerank")
    text = format_report(broken)
    assert "Реранкер не ответил на 2 из 2 вопросов" in text
    assert "Переписывание не сработало на 1 из 1 уточняющих вопросов" in text
    # Выключенное улучшение не пробуется и сбоем не считается.
    off = _without(kb, "rewrite", "rerank")
    assert (await evaluate(documents, questions, await _reset(ports), off)).fallbacks == {}


async def test_shipped_evalset_runs_and_exact_questions_are_found(
    qdrant: AsyncQdrantClient, settings: Settings
) -> None:
    """На хешах слов вместо настоящих эмбеддингов работает только лексика.

    Вопросы на точное совпадение обязаны находиться уже так; качество на вопросах «по
    смыслу», вклад реранкера и пояснений зависят от настоящих моделей и проверяются на ВМ.
    """
    documents, questions = evalset.load()
    kb = settings.kb
    report = await evaluate(documents, questions, await _ports(qdrant, kb), kb)
    assert report.total.questions == len(questions)
    assert report.enabled == ("rewrite", "context", "rerank")
    # Кандидатами становится почти весь набор, поэтому этап до реранкера измеряет полнота
    # среди первых кандидатов на глубине выдачи: она отличает потерю при поиске
    # кандидатов от потери при упорядочении.
    assert report.total.recall == 1.0 and report.total.recall_top < 1.0
    assert report.by_kind["exact"].recall_top == 1.0
    assert f"recall@{kb.search.top_k} " in format_report(report)
    exact = report.by_kind["exact"]
    assert exact.hits[kb.search.top_k] == 1.0
    # Пороги с запасом от измеренного (точные: hit@1 0.85, hit@3 1.00, MRR 0.92; все:
    # hit@1 0.75, hit@3 0.89, MRR 0.81): тест ловит заметное ухудшение правил, а не правку
    # одного вопроса набора.
    assert exact.hits[1] >= 0.8 and exact.hits[3] >= 0.9 and exact.mrr >= 0.85
    assert report.total.hits[1] >= 0.65 and report.total.hits[3] >= 0.8
    assert report.total.mrr >= 0.7

    # Переписывание — единственное улучшение, вклад которого виден уже на заглушке
    # (уточняющие вопросы: hit@1 0.33 → 0.78).
    before = _without(kb, "rewrite", "context", "rerank")
    plain = await evaluate(documents, questions, await _ports(qdrant, before), before)
    assert report.by_kind["followup"].hits[1] >= plain.by_kind["followup"].hits[1] + 0.3


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
    qdrant: AsyncQdrantClient, settings: Settings
) -> None:
    """Обязательные случаи §10.5: нужный документ первый среди однотипных, уже на лексике."""
    documents, questions = evalset.load()
    mandatory = [question for question in questions if question.text in MANDATORY]
    assert len(mandatory) == len(MANDATORY)
    assert sum(1 for document in documents if document.title.startswith("Выписка ЕГРН")) >= 4
    kb = settings.kb
    report = await evaluate(documents, mandatory, await _ports(qdrant, kb), kb)
    assert report.total.hits[1] == 1.0
