"""Поиск для ответов: реализация порта `KnowledgeBase` (docs/portal-api.md §8.4, §13.3)."""

import logging
from collections.abc import Sequence
from uuid import UUID

from portal.core.logging import code_locations
from portal.core.settings import KbSettings
from portal.kb.context import fit_sources, knowledge_context, neighbor_places, with_neighbors
from portal.kb.lexical import sparse_vector
from portal.kb.ports import (
    DialogTurn,
    Embedder,
    KnowledgeSelection,
    KnowledgeUnavailableError,
    Reranker,
    Retrieval,
    VectorIndex,
)
from portal.kb.ranking import ranked
from portal.kb.rewrite import QueryRewriter
from portal.kb.search_text import search_text
from portal.kb.store import KbUnitOfWorkFactory
from portal.llm.ports import TokenEstimator

logger = logging.getLogger(__name__)


class KnowledgeRetriever:
    """Гибридный поиск с повторной проверкой доступа в PostgreSQL.

    Фильтр доступа строится только из `user_id` и `selection`: сначала в запросе к Qdrant,
    затем в запросе к базе; выбор клиента предел доступа только сужает. Переписанный
    запрос служит только поиску и на фильтр не влияет. Текст вопроса, переписанный
    запрос, теги и найденное в журнал не попадают.
    """

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        embedder: Embedder,
        index: VectorIndex,
        rewriter: QueryRewriter,
        reranker: Reranker,
        estimator: TokenEstimator,
        settings: KbSettings,
    ) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._embedder = embedder
        self._index = index
        self._rewriter = rewriter
        self._reranker = reranker
        self._estimator = estimator
        self._settings = settings

    async def retrieve(
        self,
        query: str,
        user_id: UUID,
        selection: KnowledgeSelection,
        history: Sequence[DialogTurn] = (),
    ) -> Retrieval:
        """Найти места для ответа; любой сбой — `KnowledgeUnavailableError`.

        Сбой переписывания запроса и сбой реранкера сбоем поиска не считаются (§8.4).
        """
        try:
            return await self._search(query, user_id, selection, history)
        except Exception as error:
            # Обязательство порта: наружу выходит только одно исключение. В журнал —
            # тип и места в коде: в тексте исключения может оказаться текст вопроса.
            logger.warning(
                "knowledge search failed",
                extra={"error_type": type(error).__name__, "trace": code_locations(error)},
            )
            raise KnowledgeUnavailableError from error

    async def _search(
        self,
        query: str,
        user_id: UUID,
        selection: KnowledgeSelection,
        history: Sequence[DialogTurn],
    ) -> Retrieval:
        settings = self._settings
        limit = settings.embeddings.max_input_chars
        text = (await self._rewriter.rewrite(query[:limit], history))[:limit]
        reranker = self._reranker if settings.rerank.enabled else None
        dense = await self._embedder.embed_query(text)
        hits = await self._index.search(
            dense,
            sparse_vector(text),
            user_id,
            selection,
            settings.rerank.candidates if reranker else settings.search.top_k,
        )
        async with self._uow_factory() as uow:
            found = await uow.documents.found_fragments(user_id, selection, hits.ordered())
        by_id = {fragment.fragment_id: fragment for fragment in found}
        texts = {
            fragment.fragment_id: search_text(
                fragment.document_title,
                fragment.section,
                fragment.context,
                fragment.text,
                settings.chunking,
            )
            for fragment in found
        }
        order = await ranked(
            hits.lexical, hits.hybrid, texts, text, reranker, settings.search.top_k
        )
        rules = settings.rules_prompt.strip()
        budget = settings.context_max_tokens
        sources = fit_sources(
            [by_id[found_id] for found_id in order], rules, budget, self._estimator
        )
        if not sources:
            return Retrieval(sources=(), rules=settings.empty_rules_prompt.strip(), context="")
        selected = [by_id[source.fragment_id] for source in sources]
        distance = settings.search.neighbors
        async with self._uow_factory() as uow:
            neighbors = await uow.documents.neighbor_fragments(
                user_id, selection, neighbor_places(selected, distance)
            )
        extended = with_neighbors(
            sources, selected, neighbors, distance, rules, budget, self._estimator
        )
        return Retrieval(
            sources=tuple(sources), rules=rules, context=knowledge_context(sources, extended)
        )

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовая документация CoGIS."""
        async with self._uow_factory() as uow:
            return await uow.documents.has_cogis_documentation()
