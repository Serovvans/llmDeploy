"""Декодирование текстовых файлов (docs/portal-api.md §1.5)."""


def decode_text(data: bytes) -> str | None:
    """Декодировать текст: UTF-8, при ошибке — Windows-1251; `None` — это не текст."""
    if b"\x00" in data:
        return None
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1251")
    except UnicodeDecodeError:
        return None
