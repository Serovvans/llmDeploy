"""Порт очереди расшифровки вложений чата (docs/portal-api.md §11.7, §13.3).

Объявляет и реализует `rag-engineer`, вызывает `portal/dialogs/`.
"""

from collections.abc import Sequence
from typing import Protocol
from uuid import UUID


class TranscriptionQueue(Protocol):
    """Задания `transcribe` в очереди воркера."""

    async def enqueue(self, attachment_id: UUID) -> None:
        """Поставить задание расшифровки записи во вложении."""
        ...

    async def pending(self, attachment_ids: Sequence[UUID]) -> set[UUID]:
        """У каких из этих вложений задание в очереди есть.

        Нужно для выравнивания при старте `portal serve` (§5.8).
        """
        ...
