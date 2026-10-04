"""Системные часы."""

from datetime import UTC, datetime


class SystemClock:
    """Реализация порта `Clock` на системном времени."""

    def now(self) -> datetime:
        """Текущее время в UTC."""
        return datetime.now(UTC)
