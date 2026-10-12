"""Реранкер через Bifrost: `POST /v1/rerank` (docs/portal-api.md §12.1)."""

import asyncio
import math
from collections.abc import Sequence

import httpx

from portal.core.settings import KbRerankSettings
from portal.kb.ports import RerankUnavailableError


class BifrostReranker:
    """Реализация порта `Reranker`; запрос и тексты кандидатов в журнал не пишутся."""

    def __init__(self, client: httpx.AsyncClient, settings: KbRerankSettings, api_key: str) -> None:
        """Получить HTTP-клиент, параметры реранкера и ключ доступа к нему."""
        self._client = client
        self._settings = settings
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def scores(self, query: str, documents: Sequence[str]) -> list[float]:
        """Оценки в порядке документов; запрос и тексты обрезаются до `document_max_chars`.

        Отказ соединения, превышение срока, статус не `200` и ответ без оценки хотя бы для
        одного документа — `RerankUnavailableError`.
        """
        settings = self._settings
        limit = settings.document_max_chars
        body = {
            "model": settings.model,
            "query": query[:limit],
            "documents": [document[:limit] for document in documents],
        }
        try:
            # Срок — на весь ответ, а не на каждую его часть, как у тайм-аута клиента.
            async with asyncio.timeout(settings.timeout_seconds):
                response = await self._client.post(
                    settings.url, json=body, headers=self._headers, timeout=None
                )
        except (httpx.HTTPError, TimeoutError) as error:
            raise RerankUnavailableError from error
        if response.status_code != httpx.codes.OK:
            raise RerankUnavailableError
        try:
            by_index = {
                int(item["index"]): float(item["relevance_score"])
                for item in response.json()["results"]
            }
            scores = [by_index[index] for index in range(len(documents))]
        except (ValueError, LookupError, TypeError) as error:
            raise RerankUnavailableError from error
        if not all(math.isfinite(score) for score in scores):
            raise RerankUnavailableError
        return scores
