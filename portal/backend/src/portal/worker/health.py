"""Проверка здоровья воркера для healthcheck контейнера (docs/portal-api.md §13.4)."""

import time
from pathlib import Path


def heartbeat_is_fresh(path: Path, stale_seconds: float) -> bool:
    """Обновлял ли цикл воркера свою отметку не раньше чем `stale_seconds` назад."""
    try:
        modified = path.stat().st_mtime
    except FileNotFoundError:
        return False
    return time.time() - modified <= stale_seconds
