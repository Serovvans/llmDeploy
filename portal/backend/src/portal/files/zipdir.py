"""Оглавление ZIP до открытия архива (docs/portal-api.md §1.5, «Оглавление ZIP»)."""

import zipfile
from typing import IO


def zip_directory_bytes(file: IO[bytes]) -> int | None:
    """Размер оглавления ZIP по записи конца; `None` — записи конца нет (не ZIP).

    Запись конца читает та же функция, что затем вызывает `zipfile.ZipFile`, и у ZIP64 она
    берёт размер из записи ZIP64 (А-77). Битая запись — `zipfile.BadZipFile`.
    """
    try:
        end = zipfile._EndRecData(file)  # type: ignore[attr-defined]
    except OSError:
        return None  # как zipfile.is_zipfile: записи конца нет
    if end is None:
        return None
    size: int = end[zipfile._ECD_SIZE]  # type: ignore[attr-defined]
    return size
