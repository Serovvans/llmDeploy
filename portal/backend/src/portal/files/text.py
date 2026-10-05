"""Текстовые файлы: определение, чтение и хранение в UTF-8 (docs/portal-api.md §1.5).

Файл никогда не читается в память целиком: размер загрузки доходит до десятков
мегабайт, а одновременных загрузок может быть несколько.
"""

import codecs
import os
from pathlib import Path

_PROBE_BYTES = 64 * 1024
_CHUNK_BYTES = 1024 * 1024
_UTF8_MAX_BYTES_PER_CHAR = 4
_BOM = "\ufeff"
_FALLBACK = "cp1251"


def decode_text(data: bytes, *, complete: bool = True) -> str | None:
    """Декодировать текст: UTF-8, при ошибке — Windows-1251; `None` — это не текст.

    `complete=False` — `data` это начало файла: символ, оборванный на границе, ошибкой
    не считается.
    """
    if b"\x00" in data:
        return None
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(data, final=complete).lstrip(_BOM)
    except UnicodeDecodeError:
        pass
    try:
        return data.decode(_FALLBACK)
    except UnicodeDecodeError:
        return None


def looks_like_text(path: Path) -> bool:
    """Похоже ли начало файла на текст: нет нулевых байтов и оно декодируется."""
    with path.open("rb") as file:
        return decode_text(file.read(_PROBE_BYTES), complete=False) is not None


def read_text(path: Path, max_chars: int | None) -> str | None:
    """Текст файла, не больше `max_chars` символов; `None` — это не текст.

    С пределом читается только та часть файла, в которую эти символы заведомо входят.
    """
    with path.open("rb") as file:
        if max_chars is None:
            return decode_text(file.read())
        data = file.read(max_chars * _UTF8_MAX_BYTES_PER_CHAR + 1)
        text = decode_text(data, complete=not file.read(1))
    return None if text is None else text[:max_chars]


def store_as_utf8(path: Path) -> int:
    """Привести файл на диске к UTF-8 и вернуть его размер; читает и пишет по частям.

    Файл, который уже в UTF-8, не переписывается; иначе он считается записанным в
    Windows-1251.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        with path.open("rb") as file:
            while chunk := file.read(_CHUNK_BYTES):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        converted = path.with_name(f"{path.name}.utf8")
        fallback = codecs.getincrementaldecoder(_FALLBACK)("replace")
        with path.open("rb") as source, converted.open("wb") as target:
            while chunk := source.read(_CHUNK_BYTES):
                target.write(fallback.decode(chunk).encode())
        os.replace(converted, path)
    return path.stat().st_size
