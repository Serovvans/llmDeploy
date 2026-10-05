"""Оценка качества поиска на контрольном наборе (docs/portal-design.md §5.2).

Набор индексируется тем же разбиением, теми же эмбеддингами и тем же гибридным
запросом, что и рабочие документы, но в отдельную коллекцию; затем по каждому вопросу
проверяется, попала ли ожидаемая страница ожидаемого документа в первые k результатов.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid5

from portal.core.settings import KbSettings
from portal.kb.chunking import split_page
from portal.kb.lexical import sparse_vector
from portal.kb.ports import Embedder, FragmentPoint, VectorIndex

QuestionKind = Literal["exact", "semantic"]

# Набор не принадлежит никому из пользователей: постоянный идентификатор «владельца».
_NAMESPACE = UUID("6f0c1f2e-5d0b-4d0e-9a63-0b7f5f2f6c11")
_CUTOFFS = (1, 3)


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
    вопрос задан другими словами, чем написан документ.
    """

    text: str
    document: str
    pages: tuple[int, ...]
    kind: QuestionKind


@dataclass(frozen=True)
class EvalScore:
    """Итог по группе вопросов: доли попаданий в первые k и средний обратный ранг."""

    questions: int
    hits: dict[int, float]
    mrr: float


@dataclass(frozen=True)
class EvalReport:
    """Итог прогона: по всем вопросам и по видам; `misses` — вопросы без попадания."""

    fragments: int
    total: EvalScore
    by_kind: dict[QuestionKind, EvalScore]
    misses: tuple[str, ...]


def _score(ranks: Sequence[int | None], cutoffs: Sequence[int]) -> EvalScore:
    count = len(ranks)
    hits = {
        cutoff: sum(1 for rank in ranks if rank is not None and rank <= cutoff) / count
        for cutoff in cutoffs
    }
    mrr = sum(1 / rank for rank in ranks if rank is not None) / count
    return EvalScore(count, hits, mrr)


async def _index_documents(
    documents: Sequence[EvalDocument], embedder: Embedder, index: VectorIndex, settings: KbSettings
) -> dict[UUID, tuple[str, int]]:
    """Записать набор в индекс; результат — где лежит каждый фрагмент: документ и страница."""
    locations: dict[UUID, tuple[str, int]] = {}
    for document in documents:
        document_id = uuid5(_NAMESPACE, document.id)
        texts: list[str] = []
        fragment_ids: list[UUID] = []
        for number, page in enumerate(document.pages, start=1):
            spans = split_page(page, settings.chunking.max_chars, settings.chunking.overlap_chars)
            for start, end in spans:
                fragment_id = uuid5(document_id, f"{number}:{start}")
                locations[fragment_id] = (document.id, number)
                fragment_ids.append(fragment_id)
                texts.append(page[start:end])
        vectors = await embedder.embed_documents(texts)
        points = [
            FragmentPoint(
                fragment_id=fragment_id,
                document_id=document_id,
                scope="shared",
                owner_id=_NAMESPACE,
                is_cogis=False,
                dense=vector,
                lexical=sparse_vector(text),
            )
            for fragment_id, text, vector in zip(fragment_ids, texts, vectors, strict=True)
        ]
        await index.replace_document(document_id, points)
    return locations


async def evaluate(
    documents: Sequence[EvalDocument],
    questions: Sequence[EvalQuestion],
    embedder: Embedder,
    index: VectorIndex,
    settings: KbSettings,
) -> EvalReport:
    """Проиндексировать набор и посчитать попадания; `index` — отдельная пустая коллекция."""
    locations = await _index_documents(documents, embedder, index, settings)
    top_k = settings.search.top_k
    cutoffs = sorted({*_CUTOFFS, top_k})
    ranks: list[int | None] = []
    for question in questions:
        text = question.text[: settings.embeddings.max_input_chars]
        dense = await embedder.embed_query(text)
        found = await index.search(dense, sparse_vector(text), _NAMESPACE, "shared", top_k)
        expected = {(question.document, page) for page in question.pages}
        ranks.append(
            next(
                (
                    position
                    for position, fragment_id in enumerate(found, start=1)
                    if locations.get(fragment_id) in expected
                ),
                None,
            )
        )
    kinds: tuple[QuestionKind, ...] = ("exact", "semantic")
    by_kind = {
        kind: _score([r for r, q in zip(ranks, questions, strict=True) if q.kind == kind], cutoffs)
        for kind in kinds
        if any(question.kind == kind for question in questions)
    }
    misses = tuple(q.text for r, q in zip(ranks, questions, strict=True) if r is None)
    return EvalReport(len(locations), _score(ranks, cutoffs), by_kind, misses)


def format_report(report: EvalReport) -> str:
    """Отчёт для вывода команды `portal eval-search`."""

    def line(name: str, score: EvalScore) -> str:
        hits = "  ".join(f"hit@{cutoff} {value:.2f}" for cutoff, value in score.hits.items())
        return f"{name:<22}{score.questions:>3} вопр.  {hits}  MRR {score.mrr:.2f}"

    names = {"exact": "точное совпадение", "semantic": "по смыслу"}
    lines = [
        f"Фрагментов в наборе: {report.fragments}",
        line("все вопросы", report.total),
        *(line(names[kind], score) for kind, score in report.by_kind.items()),
    ]
    if report.misses:
        lines.append("Без попадания:")
        lines.extend(f"  - {text}" for text in report.misses)
    return "\n".join(lines) + "\n"
