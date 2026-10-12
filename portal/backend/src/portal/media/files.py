"""Файлы записей на диске глазами уборки воркера (docs/portal-api.md §1.6, §8.7).

Порт `MediaStorage` перечислять файлы не умеет, поэтому обход каталога `media/` — здесь.
"""

import re
from datetime import datetime
from pathlib import Path

_AREA = "media"
_UUID = re.compile(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}")


def stale_recording_keys(root: Path, modified_before: datetime) -> list[str]:
    """Ключи файлов `media/<owner_id>/<uuid>`, изменённых раньше `modified_before`.

    Недописанные загрузки (`*.part`) и временный каталог `media/tmp/` сюда не входят.
    """
    limit = modified_before.timestamp()
    keys: list[str] = []
    for owner in (root / _AREA).glob("*"):
        if not (_UUID.fullmatch(owner.name) and owner.is_dir()):
            continue
        for file in owner.iterdir():
            try:
                stale = _UUID.fullmatch(file.name) and file.stat().st_mtime < limit
            except FileNotFoundError:
                continue  # файл удалили, пока шёл обход
            if stale:
                keys.append(f"{_AREA}/{owner.name}/{file.name}")
    return keys
