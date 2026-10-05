"""Повтор отдельного вызова внешней службы внутри задания (docs/portal-api.md §11.2)."""

import asyncio
from collections.abc import Awaitable, Callable


async def retrying[T](
    call: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    pause_seconds: float,
    retry_on: tuple[type[Exception], ...],
) -> T:
    """Выполнить `call`; при сбое из `retry_on` повторить с паузой, всего `attempts` раз.

    Сбой последней попытки пробрасывается как есть.
    """
    attempt = 1
    while True:
        try:
            return await call()
        except retry_on:
            if attempt >= attempts:
                raise
        attempt += 1
        await asyncio.sleep(pause_seconds)
