"""Текст для поиска: по нему строятся оба вектора точки и его получает реранкер (§10.1)."""

from portal.core.settings import KbChunkingSettings
from portal.kb.chunking import one_line


def search_text(
    title: str,
    section: str | None,
    context: str | None,
    text: str,
    chunking: KbChunkingSettings,
) -> str:
    """Собрать «Документ / Раздел / пояснение / фрагмент»; отсутствующая часть пропускается.

    Название и раздел — в одну строку и не длиннее `section_max_chars`; текст — не длиннее
    `max_chars` (описание картинки может быть длиннее фрагмента). Пояснение приходит уже
    обрезанным. Наибольшая длина результата — `KbSettings.search_text_max_chars`.
    """
    head = [f"Документ: {one_line(title, chunking.section_max_chars)}"]
    if section:
        head.append(f"Раздел: {one_line(section, chunking.section_max_chars)}")
    if context:
        head.append(context)
    return "\n".join(head) + "\n\n" + text[: chunking.max_chars]
