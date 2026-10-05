"""Поиск для ответов: реализация порта `KnowledgeBase` (docs/portal-api.md §8.4, §13.3)."""

import logging
from uuid import UUID

from portal.core.logging import code_locations
from portal.core.settings import KbSettings
from portal.kb.context import fit_sources, knowledge_context
from portal.kb.lexical import sparse_vector
from portal.kb.ports import (
    Embedder,
    KnowledgeScope,
    KnowledgeUnavailableError,
    Retrieval,
    Source,
    VectorIndex,
)
from portal.kb.store import KbUnitOfWorkFactory
from portal.llm.ports import TokenEstimator

logger = logging.getLogger(__name__)


class KnowledgeRetriever:
    """Гибридный поиск с повторной проверкой доступа в PostgreSQL.

    Фильтр доступа строится только из `user_id` и `scope`: сначала в запросе к Qdrant,
    затем в запросе к базе. Текст вопроса и найденное в журнал не попадают.
    """

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        embedder: Embedder,
        index: VectorIndex,
        estimator: TokenEstimator,
        settings: KbSettings,
    ) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._embedder = embedder
        self._index = index
        self._estimator = estimator
        self._settings = settings

    async def retrieve(self, query: str, user_id: UUID, scope: KnowledgeScope) -> Retrieval:
        """Найти места для ответа; любой сбой — `KnowledgeUnavailableError`."""
        try:
            sources = await self._search(query, user_id, scope)
        except Exception as error:
            # Обязательство порта: наружу выходит только одно исключение. В журнал —
            # тип и места в коде: в тексте исключения может оказаться текст вопроса.
            logger.warning(
                "knowledge search failed",
                extra={"error_type": type(error).__name__, "trace": code_locations(error)},
            )
            raise KnowledgeUnavailableError from error
        if not sources:
            return Retrieval(
                sources=(), rules=self._settings.empty_rules_prompt.strip(), context=""
            )
        return Retrieval(
            sources=tuple(sources),
            rules=self._settings.rules_prompt.strip(),
            context=knowledge_context(sources),
        )

    async def _search(self, query: str, user_id: UUID, scope: KnowledgeScope) -> list[Source]:
        text = query[: self._settings.embeddings.max_input_chars]
        dense = await self._embedder.embed_query(text)
        found_ids = await self._index.search(
            dense, sparse_vector(text), user_id, scope, self._settings.search.top_k
        )
        async with self._uow_factory() as uow:
            found = await uow.documents.found_fragments(user_id, scope, found_ids)
        by_id = {fragment.fragment_id: fragment for fragment in found}
        ordered = [by_id[found_id] for found_id in found_ids if found_id in by_id]
        return fit_sources(
            ordered,
            self._settings.rules_prompt.strip(),
            self._settings.context_max_tokens,
            self._estimator,
        )

    async def has_cogis_documentation(self) -> bool:
        """Есть ли в общей базе готовая документация CoGIS."""
        async with self._uow_factory() as uow:
            return await uow.documents.has_cogis_documentation()
