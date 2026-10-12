"""Пояснения к фрагментам — задание `context` (docs/portal-api.md §11.5, §12.1).

Пояснение пишет модель по ограниченному окну: название документа, раздел и соседние
фрагменты. Оно — недоверенный текст и служит только поиску: в журнал не попадает.
"""

import asyncio
from collections.abc import Sequence
from functools import partial
from uuid import UUID

from portal.core.settings import KbContextSettings, KbSettings
from portal.kb import markup
from portal.kb.domain import FragmentText, Job, KbDocument
from portal.kb.lexical import sparse_vector
from portal.kb.outcomes import JobAbandonedError
from portal.kb.ports import (
    Embedder,
    EmbeddingsUnavailableError,
    FragmentPoint,
    VectorIndex,
    VectorIndexUnavailableError,
)
from portal.kb.retrying import retrying
from portal.kb.search_text import search_text
from portal.kb.store import KbUnitOfWorkFactory
from portal.llm.ports import ChatModel, ChatRequest, ModelMessage, TextPart, TokenEstimator

# Первая строка запроса зафиксирована контрактом: по ней запрос узнаёт заглушка стенда.
_INSTRUCTION = "Поясни место фрагмента в документе."


def window(
    texts: Sequence[str], position: int, budget: int, estimator: TokenEstimator
) -> list[str]:
    """Соседи фрагмента `texts[position]` в порядке документа — в пределах бюджета токенов.

    Берутся поочерёдно ближайший до и ближайший после; сторона, где очередной сосед не
    поместился или текст кончился, дальше не расширяется.
    """
    before: list[str] = []
    after: list[str] = []
    sides = {-1: before, 1: after}
    used = 0
    step = 1
    while sides:
        for direction in tuple(sides):
            index = position + direction * step
            cost = estimator.text(texts[index]) if 0 <= index < len(texts) else budget + 1
            if used + cost > budget:
                del sides[direction]
                continue
            used += cost
            sides[direction].append(texts[index])
        step += 1
    return [*reversed(before), *after]


class FragmentExplainer:
    """Запрос пояснения к одному фрагменту."""

    def __init__(
        self,
        model: ChatModel,
        estimator: TokenEstimator,
        settings: KbContextSettings,
        slots: asyncio.Semaphore,
    ) -> None:
        """Получить модель, оценку токенов и параметры.

        `slots` — общий на весь воркер предел фоновых обращений к основной модели
        (`kb.worker.background_llm_parallel`).
        """
        self._model = model
        self._estimator = estimator
        self._settings = settings
        self._slots = slots

    async def explain(
        self, title: str, section: str | None, texts: Sequence[str], position: int
    ) -> str:
        """Пояснение к `texts[position]`; `texts` — фрагменты документа по порядку.

        Ответ — в одну строку, не длиннее `kb.context.max_chars`; пустой ответ — пустая
        строка. Сбой модели пробрасывается.
        """
        settings = self._settings
        neighbors = window(texts, position, settings.window_tokens, self._estimator)
        parts = [_INSTRUCTION, markup.inline(markup.DOCUMENT, title)]
        if section:
            parts.append(markup.inline(markup.SECTION, section))
        parts.append(markup.block(markup.NEIGHBORS, "\n".join(neighbors)))
        parts.append(markup.block(markup.FRAGMENT, texts[position]))
        request = ChatRequest(
            messages=(
                ModelMessage("system", (TextPart(settings.system_prompt),)),
                ModelMessage("user", (TextPart("\n".join(parts)),)),
            ),
            reasoning_effort="low",
            max_tokens=settings.max_tokens,
        )
        async with self._slots:
            answer = await self._model.complete(request)
        # PostgreSQL не хранит нулевой символ в тексте.
        return " ".join(answer.replace("\x00", "").split())[: settings.max_chars]


class Contextualizer:
    """Выполняет задание `context`: одно взятие — одна порция фрагментов."""

    def __init__(
        self,
        uow_factory: KbUnitOfWorkFactory,
        explainer: FragmentExplainer,
        embedder: Embedder,
        index: VectorIndex,
        settings: KbSettings,
    ) -> None:
        """Получить зависимости явно."""
        self._uow_factory = uow_factory
        self._explainer = explainer
        self._embedder = embedder
        self._index = index
        self._settings = settings

    async def run(self, job: Job) -> bool:
        """Дописать пояснения очередной порции; `False` — задание выполнено и снято.

        При выключенном `kb.context.enabled` работа не делается. Сбой модели на одном из
        фрагментов прекращает порцию и пробрасывается, но уже полученные пояснения
        сохраняются: повтор задания их не запрашивает заново.
        """
        context = self._settings.context
        async with self._uow_factory() as uow:
            document = await uow.documents.lock(job.document_id)
            if document is None:
                raise JobAbandonedError
            batch = (
                await uow.documents.fragments_without_context(document.id, context.batch_size)
                if context.enabled
                else []
            )
            if not batch:
                # Задание снимается под той же блокировкой, под которой завершение
                # индексации ставит новое (`add_if_absent`): иначе новые фрагменты
                # документа, появившиеся между проверкой и снятием, остались бы без пояснений.
                await uow.jobs.finish(job.id)
                return False
            # Окно ограничено токенами; из базы соседи читаются с запасом в размер порции.
            around = await uow.documents.fragment_texts(
                document.id,
                batch[0].ordinal - context.batch_size,
                batch[-1].ordinal + context.batch_size,
            )
        explained, failure = await self._explain(document.title, batch, around)
        if explained:
            await self._save(document, batch, explained)
        if failure is not None:
            raise failure
        return True

    async def _explain(
        self, title: str, batch: Sequence[FragmentText], around: Sequence[FragmentText]
    ) -> tuple[dict[UUID, str], BaseException | None]:
        """Пояснения порции и первый сбой, если он был: после него запросы прекращаются."""
        texts = [fragment.text for fragment in around]
        positions = {fragment.id: position for position, fragment in enumerate(around)}
        tasks = {
            fragment.id: asyncio.create_task(
                self._explainer.explain(title, fragment.section, texts, positions[fragment.id])
            )
            for fragment in batch
        }
        try:
            await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_EXCEPTION)
        finally:
            for task in tasks.values():
                task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        finished = {key: task for key, task in tasks.items() if not task.cancelled()}
        failure = next((e for task in finished.values() if (e := task.exception())), None)
        explained = {key: task.result() for key, task in finished.items() if not task.exception()}
        return explained, failure

    async def _save(
        self, document: KbDocument, batch: Sequence[FragmentText], explained: dict[UUID, str]
    ) -> None:
        """Пояснения в базу и векторы с ними в Qdrant — одной транзакцией под блокировкой.

        Фрагмент, которого уже нет (документ тем временем переобработан), пропускается.
        """
        worker = self._settings.worker
        fragments = [fragment for fragment in batch if fragment.id in explained]
        texts = [
            search_text(
                document.title,
                fragment.section,
                explained[fragment.id],
                fragment.text,
                self._settings.chunking,
            )
            for fragment in fragments
        ]
        vectors = await retrying(
            partial(self._embedder.embed_documents, texts),
            attempts=worker.call_attempts,
            pause_seconds=worker.call_retry_pause_seconds,
            retry_on=(EmbeddingsUnavailableError,),
        )
        points = [
            FragmentPoint(
                fragment_id=fragment.id,
                document_id=document.id,
                scope=document.scope,
                owner_id=document.owner_id,
                is_cogis=document.is_cogis,
                tags=(),  # payload этап контекста не меняет
                dense=vector,
                lexical=sparse_vector(text),
            )
            for fragment, text, vector in zip(fragments, texts, vectors, strict=True)
        ]
        async with self._uow_factory() as uow:
            if await uow.documents.lock(document.id) is None:
                raise JobAbandonedError
            kept = await uow.documents.set_contexts(document.id, explained)
            await retrying(
                partial(
                    self._index.update_vectors,
                    [point for point in points if point.fragment_id in kept],
                ),
                attempts=worker.call_attempts,
                pause_seconds=worker.call_retry_pause_seconds,
                retry_on=(VectorIndexUnavailableError,),
            )
