"""Блок найденных мест для запроса к модели (docs/portal-api.md §8.4, §13.3); чистые функции.

Содержимое и названия документов — недоверенные данные: они идут в размеченном блоке
сообщения пользователя и не могут закрыть свою разметку.
"""

from collections.abc import Mapping, Sequence
from itertools import pairwise
from uuid import UUID

from portal.kb.domain import FoundFragment
from portal.kb.markup import FRAGMENT, KNOWLEDGE, block, inert
from portal.kb.ports import Source
from portal.llm.ports import TokenEstimator

# Квадратные скобки в названии файла читались бы как ещё один номер источника.
_BRACKETS = str.maketrans("[]", "()")
_NO_TEXTS: Mapping[UUID, str] = {}


def _place(source: Source) -> str:
    """Место источника в строке-заголовке: страница, таймкод записи, отметка картинки (§8.4)."""
    if source.timecode_seconds is not None:
        minutes, seconds = divmod(source.timecode_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f", запись, {hours:02}:{minutes:02}:{seconds:02}"
    page = f", стр. {source.page}" if source.page is not None else ""
    return f"{page}, изображение" if source.image_id is not None else page


def _source_block(source: Source, text: str) -> str:
    title = inert(source.document_title).replace("\n", " ").translate(_BRACKETS)
    return f"[{source.n}] {title}{_place(source)}\n{block(FRAGMENT, text)}"


def knowledge_context(sources: Sequence[Source], texts: Mapping[UUID, str] = _NO_TEXTS) -> str:
    """Размеченный блок с найденными местами; без источников — пустая строка.

    Каждое место начинается строкой `[n] <название документа>, стр. <N>` (без «стр.»,
    если у документа нет страниц; у записи — `запись, ЧЧ:ММ:СС`, у картинки — с отметкой
    `изображение`), его текст заключён в свои пометки. Название стоит вне
    пометок, поэтому квадратные скобки в нём заменяются круглыми: номер в строке один.
    `texts` — текст места вместе с соседними фрагментами, по идентификатору фрагмента;
    места без записи идут своей цитатой.
    """
    if not sources:
        return ""
    body = "\n\n".join(
        _source_block(source, texts.get(source.fragment_id, source.quote)) for source in sources
    )
    return f"<{KNOWLEDGE}>\n{body}\n</{KNOWLEDGE}>"


def _fits(
    sources: Sequence[Source],
    texts: Mapping[UUID, str],
    rules: str,
    budget: int,
    estimator: TokenEstimator,
) -> bool:
    return estimator.text(rules) + estimator.text(knowledge_context(sources, texts)) <= budget


def fit_sources(
    found: Sequence[FoundFragment], rules: str, budget: int, estimator: TokenEstimator
) -> list[Source]:
    """Источники по порядку выдачи, пока правила и блок вместе укладываются в бюджет.

    Номера — 1, 2, 3 … без пропусков. Фрагмент, который не поместился, и все следующие
    за ним не передаются: текст фрагмента не обрезается.
    """
    sources: list[Source] = []
    for fragment in found:
        candidate = Source(
            n=len(sources) + 1,
            document_id=fragment.document_id,
            document_title=fragment.document_title,
            scope=fragment.scope,
            page=fragment.page,
            timecode_seconds=None if fragment.start_ms is None else fragment.start_ms // 1000,
            image_id=fragment.image_id,
            fragment_id=fragment.fragment_id,
            quote=fragment.text,
        )
        if not _fits([*sources, candidate], _NO_TEXTS, rules, budget, estimator):
            break
        sources.append(candidate)
    return sources


def neighbor_places(selected: Sequence[FoundFragment], distance: int) -> list[tuple[UUID, int]]:
    """Какие соседи нужны отобранным: (документ, порядковый номер), без самих отобранных.

    Фрагмент-картинка соседей не получает (§8.4, шаг 7).
    """
    taken = {(fragment.document_id, fragment.ordinal) for fragment in selected}
    places = {
        (fragment.document_id, fragment.ordinal + shift)
        for fragment in selected
        if fragment.image_id is None
        for shift in range(-distance, distance + 1)
    }
    return sorted(places - taken)


def _joined(pieces: Sequence[FoundFragment]) -> str:
    """Текст фрагментов в порядке документа; перекрывающийся текст не повторяется."""
    text = pieces[0].text
    for previous, piece in pairwise(pieces):
        overlap = previous.end_offset - piece.start_offset
        if piece.page_number == previous.page_number and overlap > 0:
            text += piece.text[overlap:]
        else:
            text += "\n" + piece.text
    return text


def with_neighbors(
    sources: Sequence[Source],
    selected: Sequence[FoundFragment],
    neighbors: Sequence[FoundFragment],
    distance: int,
    rules: str,
    budget: int,
    estimator: TokenEstimator,
) -> dict[UUID, str]:
    """Тексты мест с соседними фрагментами — на остаток бюджета (§8.4, шаг 7).

    `selected` — фрагменты источников в том же порядке. Соседи добавляются в порядке
    выдачи, ближние раньше дальних; сосед, который сам вошёл в выдачу или уже добавлен к
    другому месту, не дублируется, а дальше него в ту же сторону место не расширяется.
    Первый не поместившийся сосед останавливает добавление. Цитата источника не меняется.
    """
    by_place = {(fragment.document_id, fragment.ordinal): fragment for fragment in neighbors}
    taken = {(fragment.document_id, fragment.ordinal) for fragment in selected}
    texts: dict[UUID, str] = {}
    for fragment in selected[: len(sources)]:
        if fragment.image_id is not None:
            continue
        pieces = [fragment]
        directions = [-1, 1]
        for step in range(1, distance + 1):
            for direction in tuple(directions):
                place = (fragment.document_id, fragment.ordinal + direction * step)
                if place in taken or place not in by_place:
                    directions.remove(direction)
                    continue
                extended = sorted([*pieces, by_place[place]], key=lambda piece: piece.ordinal)
                candidate = {**texts, fragment.fragment_id: _joined(extended)}
                if not _fits(sources, candidate, rules, budget, estimator):
                    return texts
                pieces, texts = extended, candidate
                taken.add(place)
    return texts
