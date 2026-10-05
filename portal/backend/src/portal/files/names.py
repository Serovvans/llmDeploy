"""Имя загруженного файла для показа (docs/portal-api.md §1.5)."""

import re

_MAX_LENGTH = 255
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def display_file_name(raw: str) -> str:
    """Имя для показа: последняя часть пути, без управляющих символов, до 255 символов.

    Путь на диске из него не строится никогда.
    """
    name = _CONTROL_CHARACTERS.sub("", raw.replace("\\", "/").rsplit("/", 1)[-1]).strip()
    return name[:_MAX_LENGTH] or "файл"
