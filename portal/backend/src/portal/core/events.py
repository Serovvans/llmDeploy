"""Поток событий от сценария к клиенту (docs/portal-api.md §6.1), без веб-фреймворка."""

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

KEEPALIVE_LINE = ": keep-alive\n\n"


def format_event(event: str, data: Mapping[str, Any]) -> str:
    """Одно событие: имя и одна строка JSON."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class EventChannel:
    """Канал между сценарием, который формирует ответ, и соединением клиента.

    Сценарий живёт в своей задаче и не зависит от соединения: закрытие соединения он
    узнаёт по `consumer_gone` и сам решает, что сохранить.
    """

    def __init__(self) -> None:
        """Создать пустой канал."""
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.consumer_gone = asyncio.Event()

    def emit(self, event: str, data: Mapping[str, Any]) -> None:
        """Отправить событие клиенту."""
        self._queue.put_nowait(format_event(event, data))

    def finish(self) -> None:
        """Завершить поток: после этого сервер закрывает соединение."""
        self._queue.put_nowait(None)

    async def stream(self, keepalive_seconds: float) -> AsyncIterator[str]:
        """Строки потока для клиента; в паузах — строка `: keep-alive`.

        Закрытие соединения прерывает этот генератор в любой точке ожидания; блок
        `finally` сообщает об этом сценарию.
        """
        try:
            while True:
                try:
                    item = await asyncio.wait_for(self._queue.get(), keepalive_seconds)
                except TimeoutError:
                    yield KEEPALIVE_LINE
                    continue
                if item is None:
                    return
                yield item
        finally:
            self.consumer_gone.set()
