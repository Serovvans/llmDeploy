"""Эмбеддинги через Bifrost: `POST /v1/embeddings` (docs/portal-api.md §12.1)."""

from collections.abc import Sequence
from itertools import batched

import httpx

from portal.core.settings import KbEmbeddingsSettings
from portal.kb.ports import EmbeddingsUnavailableError


class BifrostEmbedder:
    """Реализация порта `Embedder`; тексты запросов и ответы в журнал не пишутся."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        model: str,
        api_key: str,
        settings: KbEmbeddingsSettings,
    ) -> None:
        """Получить HTTP-клиент, адрес Bifrost, имя модели, ключ портала и параметры."""
        self._client = client
        self._url = f"{base_url.rstrip('/')}/embeddings"
        self._model = model
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._settings = settings

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Векторы фрагментов: не больше `batch_size` текстов за запрос."""
        vectors: list[list[float]] = []
        for batch in batched(texts, self._settings.batch_size):
            vectors.extend(await self._request(list(batch)))
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        """Вектор запроса; перед текстом — инструкция модели эмбеддингов, если она задана."""
        return (await self._request([self._settings.query_instruction + text]))[0]

    async def _request(self, inputs: list[str]) -> list[list[float]]:
        try:
            response = await self._client.post(
                self._url,
                json={"model": self._model, "input": inputs},
                headers=self._headers,
                timeout=self._settings.timeout_seconds,
            )
        except httpx.HTTPError as error:
            raise EmbeddingsUnavailableError from error
        if response.status_code != httpx.codes.OK:
            raise EmbeddingsUnavailableError
        try:
            items = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors = [[float(value) for value in item["embedding"]] for item in items]
        except (ValueError, LookupError, TypeError) as error:
            raise EmbeddingsUnavailableError from error
        dimension = self._settings.dimension
        if len(vectors) != len(inputs) or any(len(vector) != dimension for vector in vectors):
            raise EmbeddingsUnavailableError
        return vectors
