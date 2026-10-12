"""Оценка качества поиска на контрольном наборе (docs/portal-design.md §5.2).

Набор проходит тот же путь, что и рабочие документы, — разбиение по структуре, текст для
поиска, пояснения, эмбеддинги, гибридный запрос, переписывание уточняющих вопросов,
реранкер — но в отдельной коллекции и без базы. Метрики считаются по этапам: полнота
среди кандидатов — до реранкера, попадания в первые k, MRR и nDCG — по итоговой выдаче;
так видно, какой этап теряет ответ.
"""

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args
from uuid import UUID, uuid5

from portal.core.settings import KbSettings
from portal.kb.chunking import split_document
from portal.kb.explaining import FragmentExplainer
from portal.kb.lexical import sparse_vector
from portal.kb.ports import (
    DialogTurn,
    Embedder,
    FragmentPoint,
    KnowledgeSelection,
    Reranker,
    RerankUnavailableError,
    VectorIndex,
)
from portal.kb.ranking import ranked
from portal.kb.rewrite import QueryRewriter
from portal.kb.search_text import search_text

QuestionKind = Literal["exact", "semantic", "followup"]
Feature = Literal["rewrite", "context", "rerank"]
FEATURES: tuple[Feature, ...] = get_args(Feature)
KINDS: tuple[QuestionKind, ...] = get_args(QuestionKind)

# Набор не принадлежит никому из пользователей: постоянный идентификатор «владельца».
_NAMESPACE = UUID("6f0c1f2e-5d0b-4d0e-9a63-0b7f5f2f6c11")
_SHARED = KnowledgeSelection(frozenset({"shared"}))
_CUTOFFS = (1, 3)
_FALLBACK_LINES: dict[Feature, str] = {
    "rewrite": (
        "Переписывание не сработало на {failed} из {tried} уточняющих вопросов:"
        " поиск шёл по исходному вопросу."
    ),
    "rerank": (
        "Реранкер не ответил на {failed} из {tried} вопросов: их выдача — в порядке слияния ветвей."
    ),
}
_KIND_NAMES: dict[QuestionKind, str] = {
    "exact": "точное совпадение",
    "semantic": "по смыслу",
    "followup": "уточняющие",
}


@dataclass(frozen=True)
class EvalDocument:
    """Документ набора: страницы — готовый текст, без разбора файлов."""

    id: str
    title: str
    pages: tuple[str, ...]


@dataclass(frozen=True)
class EvalQuestion:
    """Вопрос набора и то, где лежит ответ.

    `kind`: `exact` — ответ находится по номеру, реквизиту или фамилии; `semantic` —
    вопрос задан другими словами, чем написан документ; `followup` — уточняющий вопрос,
    понятный только вместе с `history` — предыдущими сообщениями диалога.
    """

    text: str
    document: str
    pages: tuple[int, ...]
    kind: QuestionKind
    history: tuple[DialogTurn, ...] = ()


@dataclass(frozen=True)
class EvalPorts:
    """Чем пользуется прогон: те же порты и шаги, что у рабочего поиска и индексации."""

    embedder: Embedder
    index: VectorIndex  # отдельная пустая коллекция
    rewriter: QueryRewriter
    explainer: FragmentExplainer
    reranker: Reranker


@dataclass(frozen=True)
class EvalScore:
    """Итог по группе вопросов.

    `recall` — доля вопросов, ответ на которые есть среди кандидатов (до реранкера);
    `recall_top` — то же среди первых кандидатов на глубине выдачи, в порядке слияния
    ветвей: на малом наборе, где кандидатами становятся почти все фрагменты, этап до
    реранкера измеряет она. `hits`, `mrr`, `ndcg` — по итоговой выдаче: доли попаданий в
    первые k, средний обратный ранг и nDCG на глубине выдачи.
    """

    questions: int
    recall: float
    recall_top: float
    hits: dict[int, float]
    mrr: float
    ndcg: float


@dataclass(frozen=True)
class EvalReport:
    """Итог прогона: по всем вопросам и по видам.

    `lost_in_candidates` — вопросы, ответа на которые не было уже среди кандидатов;
    `lost_in_ranking` — кандидат был, но в выдачу не вошёл. `fallbacks` — у включённого
    улучшения: на скольких вопросах оно не сработало и поиск молча пошёл без него, и на
    скольких пробовалось. Переписывание пробуется на уточняющих вопросах, «не сработало» —
    вопрос остался прежним (сбой, тайм-аут, пустой ответ); реранкер — не ответил.
    """

    fragments: int
    candidates: int
    enabled: tuple[Feature, ...]
    total: EvalScore
    by_kind: dict[QuestionKind, EvalScore]
    lost_in_candidates: tuple[str, ...]
    lost_in_ranking: tuple[str, ...]
    fallbacks: dict[Feature, tuple[int, int]]


@dataclass(frozen=True)
class _Outcome:
    """Один вопрос: был ли ответ среди кандидатов, его место в выдаче и nDCG выдачи.

    `rewritten` — изменило ли вопрос переписывание; `None` — оно не пробовалось.
    """

    in_candidates: bool
    in_top_candidates: bool
    rank: int | None
    ndcg: float
    rewritten: bool | None


class _WatchedReranker:
    """Реранкер прогона: считает вопросы, на которых он не ответил.

    Рабочий поиск при сбое реранкера молча отдаёт порядок слияния ветвей; в замере такой
    откат неотличим от «реранкер ничего не меняет», поэтому он считается.
    """

    def __init__(self, reranker: Reranker) -> None:
        """Запомнить настоящий реранкер."""
        self._reranker = reranker
        self.asked = 0
        self.failed = 0

    async def scores(self, query: str, documents: Sequence[str]) -> list[float]:
        """Оценки настоящего реранкера; его сбой учитывается и пробрасывается."""
        self.asked += 1
        try:
            return await self._reranker.scores(query, documents)
        except RerankUnavailableError:
            self.failed += 1
            raise


def _score(outcomes: Sequence[_Outcome], cutoffs: Sequence[int]) -> EvalScore:
    count = len(outcomes)
    ranks = [outcome.rank for outcome in outcomes]
    return EvalScore(
        questions=count,
        recall=sum(outcome.in_candidates for outcome in outcomes) / count,
        recall_top=sum(outcome.in_top_candidates for outcome in outcomes) / count,
        hits={
            cutoff: sum(1 for rank in ranks if rank is not None and rank <= cutoff) / count
            for cutoff in cutoffs
        },
        mrr=sum(1 / rank for rank in ranks if rank is not None) / count,
        ndcg=sum(outcome.ndcg for outcome in outcomes) / count,
    )


def _ndcg(relevant: Sequence[bool], relevant_total: int, depth: int) -> float:
    """Метрика nDCG выдачи глубиной `depth` при двоичной значимости.

    `relevant` — значим ли фрагмент на каждом месте выдачи; `relevant_total` — сколько
    значимых фрагментов в наборе всего.
    """
    ideal = sum(1 / math.log2(place + 2) for place in range(min(relevant_total, depth)))
    gain = sum(1 / math.log2(place + 2) for place, hit in enumerate(relevant) if hit)
    return gain / ideal if ideal else 0.0


@dataclass(frozen=True)
class _Indexed:
    """Проиндексированный набор: где лежит каждый фрагмент и его текст для поиска."""

    locations: dict[UUID, tuple[str, int]]
    texts: dict[UUID, str]


async def _index_documents(
    documents: Sequence[EvalDocument], ports: EvalPorts, settings: KbSettings
) -> _Indexed:
    """Записать набор в индекс тем же разбиением и тем же текстом для поиска."""
    chunking = settings.chunking
    indexed = _Indexed({}, {})
    for document in documents:
        document_id = uuid5(_NAMESPACE, document.id)
        spans = split_document(
            list(enumerate(document.pages, start=1)),
            chunking.heading_patterns,
            chunking.max_chars,
            chunking.overlap_chars,
            chunking.section_max_chars,
        )
        fragment_ids = [uuid5(document_id, f"{span.page_number}:{span.start}") for span in spans]
        plain = [document.pages[span.page_number - 1][span.start : span.end] for span in spans]
        contexts: list[str | None] = [None] * len(spans)
        if settings.context.enabled:
            contexts = list(
                await asyncio.gather(
                    *(
                        ports.explainer.explain(document.title, span.section, plain, position)
                        for position, span in enumerate(spans)
                    )
                )
            )
        texts = [
            search_text(document.title, span.section, context, text, chunking)
            for span, context, text in zip(spans, contexts, plain, strict=True)
        ]
        vectors = await ports.embedder.embed_documents(texts)
        points = [
            FragmentPoint(
                fragment_id=fragment_id,
                document_id=document_id,
                scope="shared",
                owner_id=_NAMESPACE,
                is_cogis=False,
                tags=(),
                dense=vector,
                lexical=sparse_vector(text),
            )
            for fragment_id, text, vector in zip(fragment_ids, texts, vectors, strict=True)
        ]
        await ports.index.stage_document(document_id, points)
        await ports.index.publish_document(document_id, ())
        for fragment_id, span, text in zip(fragment_ids, spans, texts, strict=True):
            indexed.locations[fragment_id] = (document.id, span.page_number)
            indexed.texts[fragment_id] = text
    return indexed


async def _ask(
    question: EvalQuestion,
    indexed: _Indexed,
    ports: EvalPorts,
    reranker: Reranker | None,
    settings: KbSettings,
) -> _Outcome:
    """Один вопрос тем же путём, что в рабочем поиске (§8.4, шаги 1–6)."""
    limit = settings.embeddings.max_input_chars
    top_k = settings.search.top_k
    asked = question.text[:limit]
    text = (await ports.rewriter.rewrite(asked, question.history))[:limit]
    rewriting = settings.rewrite.enabled and bool(question.history)
    hits = await ports.index.search(
        await ports.embedder.embed_query(text),
        sparse_vector(text),
        _NAMESPACE,
        _SHARED,
        settings.rerank.candidates if reranker else top_k,
    )
    expected = {(question.document, page) for page in question.pages}
    relevant = {found for found, place in indexed.locations.items() if place in expected}
    final = await ranked(hits.lexical, hits.hybrid, indexed.texts, text, reranker, top_k)
    candidates = hits.ordered()
    return _Outcome(
        in_candidates=any(found in relevant for found in candidates),
        in_top_candidates=any(found in relevant for found in candidates[:top_k]),
        rank=next((n for n, found in enumerate(final, start=1) if found in relevant), None),
        ndcg=_ndcg([found in relevant for found in final], len(relevant), top_k),
        rewritten=text != asked if rewriting else None,
    )


async def evaluate(
    documents: Sequence[EvalDocument],
    questions: Sequence[EvalQuestion],
    ports: EvalPorts,
    settings: KbSettings,
) -> EvalReport:
    """Проиндексировать набор и посчитать метрики по этапам.

    Улучшения поиска включаются флагами `settings`: `rewrite.enabled`, `context.enabled`,
    `rerank.enabled` — прогон с выключенным флагом даёт замер «до».
    """
    indexed = await _index_documents(documents, ports, settings)
    top_k = settings.search.top_k
    cutoffs = sorted({*_CUTOFFS, top_k})
    reranker = _WatchedReranker(ports.reranker) if settings.rerank.enabled else None
    outcomes = [await _ask(question, indexed, ports, reranker, settings) for question in questions]
    rewrites = [outcome.rewritten for outcome in outcomes if outcome.rewritten is not None]
    fallbacks: dict[Feature, tuple[int, int]] = {}
    if rewrites:
        fallbacks["rewrite"] = (rewrites.count(False), len(rewrites))
    if reranker is not None and reranker.asked:
        fallbacks["rerank"] = (reranker.failed, reranker.asked)
    by_kind = {
        kind: _score(
            [o for o, q in zip(outcomes, questions, strict=True) if q.kind == kind], cutoffs
        )
        for kind in _KIND_NAMES
        if any(question.kind == kind for question in questions)
    }
    asked = list(zip(outcomes, questions, strict=True))
    flags = {
        "rewrite": settings.rewrite.enabled,
        "context": settings.context.enabled,
        "rerank": settings.rerank.enabled,
    }
    return EvalReport(
        fragments=len(indexed.locations),
        candidates=settings.rerank.candidates if settings.rerank.enabled else top_k,
        enabled=tuple(feature for feature in FEATURES if flags[feature]),
        total=_score(outcomes, cutoffs),
        by_kind=by_kind,
        lost_in_candidates=tuple(q.text for o, q in asked if not o.in_candidates),
        lost_in_ranking=tuple(q.text for o, q in asked if o.in_candidates and o.rank is None),
        fallbacks=fallbacks,
    )


def idle_features(report: EvalReport) -> tuple[Feature, ...]:
    """Включённые улучшения, не сработавшие ни на одном вопросе: замер с ними неверен."""
    return tuple(
        feature for feature, (failed, tried) in report.fallbacks.items() if failed == tried
    )


def format_report(report: EvalReport) -> str:
    """Отчёт для вывода команды `portal eval-search`."""
    depth = max(report.total.hits)

    def line(name: str, score: EvalScore) -> str:
        hits = "  ".join(f"hit@{cutoff} {value:.2f}" for cutoff, value in score.hits.items())
        recall = f"recall@{report.candidates} {score.recall:.2f}"
        if report.candidates > depth:
            recall += f"  recall@{depth} {score.recall_top:.2f}"
        return (
            f"{name:<22}{score.questions:>3} вопр.  {recall}"
            f"  |  {hits}  MRR {score.mrr:.2f}  nDCG@{depth} {score.ndcg:.2f}"
        )

    lines = [
        f"Фрагментов в наборе: {report.fragments}",
        f"Включено: {', '.join(report.enabled) or 'ничего'}",
        "Слева — кандидаты до реранкера (все и первые по слиянию ветвей), справа — итоговая"
        " выдача.",
        line("все вопросы", report.total),
        *(line(_KIND_NAMES[kind], score) for kind, score in report.by_kind.items()),
    ]
    for feature, template in _FALLBACK_LINES.items():
        failed, tried = report.fallbacks.get(feature, (0, 0))
        if failed:
            lines.append(template.format(failed=failed, tried=tried))
    for title, texts in (
        ("Ответа нет среди кандидатов:", report.lost_in_candidates),
        ("Кандидат был, в выдачу не вошёл:", report.lost_in_ranking),
    ):
        if texts:
            lines.append(title)
            lines.extend(f"  - {text}" for text in texts)
    return "\n".join(lines) + "\n"
