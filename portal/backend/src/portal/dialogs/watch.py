"""Наблюдение за открытым потоком: соединение клиента, срок, сессия, сам ответ.

Одно и то же для потока ответа и потока разбора документа (docs/portal-api.md §2.6, §5.5):
закрытие соединения — остановка; удаление сессии или диалога и выход срока обрывают
работу, и запрос к модели отменяется.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any, Literal
from uuid import UUID

from portal.core.events import EventChannel
from portal.core.ports import SessionAuthenticator
from portal.llm.ports import (
    ChatModel,
    ChatRequest,
    ContentDelta,
    Finished,
    ModelUnavailableError,
    ReasoningDelta,
)

Interruption = Literal["stopped", "generation_timeout", "session_ended", "dialog_deleted"]

INTERRUPTION_MESSAGES: dict[str, str] = {
    "generation_timeout": "Ответ формировался слишком долго и был прерван.",
    "session_ended": "Сеанс завершён. Войдите снова.",
    "dialog_deleted": "Диалог удалён.",
}


class StreamWatch:
    """Следит за потоком, пока сценарий ждёт модель или поиск."""

    def __init__(
        self,
        channel: EventChannel,
        sessions: SessionAuthenticator,
        session_id: UUID,
        subject_exists: Callable[[], Awaitable[bool]],
        timeout_seconds: float,
        recheck_seconds: float,
    ) -> None:
        """Начать наблюдение; `subject_exists` — существует ли ещё то, что формируется."""
        self._gone = asyncio.create_task(channel.consumer_gone.wait())
        self._sessions = sessions
        self._session_id = session_id
        self._subject_exists = subject_exists
        self._recheck_seconds = recheck_seconds
        self.started = time.monotonic()
        self._deadline = self.started + timeout_seconds
        self._next_check = self.started + recheck_seconds

    def close(self) -> None:
        """Закончить наблюдение."""
        self._gone.cancel()

    async def wait(self, pending: asyncio.Future[Any]) -> Interruption | None:
        """Дождаться `pending`; вернуть причину, если работу нужно прервать раньше."""
        while True:
            pause = max(0.0, min(self._next_check, self._deadline) - time.monotonic())
            await asyncio.wait(
                {pending, self._gone}, timeout=pause, return_when=asyncio.FIRST_COMPLETED
            )
            now = time.monotonic()
            if self._gone.done():
                return "stopped"
            if now >= self._deadline:
                return "generation_timeout"
            if now >= self._next_check:
                self._next_check = now + self._recheck_seconds
                if not await self._sessions.session_exists(self._session_id):
                    return "session_ended"
                if not await self._subject_exists():
                    return "dialog_deleted"
            if pending.done():
                return None

    async def result[T](self, awaitable: Awaitable[T]) -> tuple[T | None, Interruption | None]:
        """Выполнить `awaitable` под наблюдением; при прерывании он отменяется."""
        pending = asyncio.ensure_future(awaitable)
        try:
            reason = await self.wait(pending)
            if reason is not None:
                return None, reason
            return pending.result(), None
        finally:
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)


async def watched_stream(
    model: ChatModel, request: ChatRequest, watch: StreamWatch
) -> AsyncGenerator[ReasoningDelta | ContentDelta | Finished | Interruption]:
    """Поток модели под наблюдением.

    Отдаёт события модели; если работу нужно прервать — причину (строкой) последним
    элементом. Поток, кончившийся без `Finished`, полным не считается никогда: это
    `ModelUnavailableError`. Закрытие генератора отменяет запрос к модели, поэтому
    пользоваться им нужно через `contextlib.aclosing`.
    """
    stream = model.stream(request)
    pending: asyncio.Future[Any] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(anext(stream))
            if (reason := await watch.wait(pending)) is not None:
                yield reason
                return
            try:
                event = pending.result()
            except StopAsyncIteration:
                raise ModelUnavailableError from None
            yield event
            if isinstance(event, Finished):
                return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()
