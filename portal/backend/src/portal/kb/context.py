"""Блок найденных мест для запроса к модели (docs/portal-api.md §8.4, §13.3); чистые функции.

Содержимое и названия документов — недоверенные данные: они идут в размеченном блоке
сообщения пользователя и не могут закрыть свою разметку.
"""

import re
from collections.abc import Sequence

from portal.kb.domain import FoundFragment
from portal.kb.ports import Source
from portal.llm.ports import TokenEstimator

# Имена пометок упоминаются в правилах `kb.rules_prompt`.
_BLOCK = "база_знаний"
_FRAGMENT = "фрагмент"
# Закрывающая пометка любого из двух видов в любом написании: с пробелами и в любом регистре.
_CLOSING_TAG = re.compile(rf"<\s*/\s*({_BLOCK}|{_FRAGMENT})", re.IGNORECASE)


def _inert(text: str) -> str:
    """Не дать содержимому закрыть свой блок данных: разметка блока — только наша."""
    return _CLOSING_TAG.sub(r"<\\/\1", text)


# Квадратные скобки в названии файла читались бы как ещё один номер источника.
_BRACKETS = str.maketrans("[]", "()")


def _source_block(source: Source) -> str:
    title = _inert(source.document_title).replace("\n", " ").translate(_BRACKETS)
    page = f", стр. {source.page}" if source.page is not None else ""
    return f"[{source.n}] {title}{page}\n<{_FRAGMENT}>\n{_inert(source.quote)}\n</{_FRAGMENT}>"


def knowledge_context(sources: Sequence[Source]) -> str:
    """Размеченный блок с найденными местами; без источников — пустая строка.

    Каждое место начинается строкой `[n] <название документа>, стр. <N>` (без «стр.»,
    если у документа нет страниц), его текст заключён в свои пометки. Название стоит вне
    пометок, поэтому квадратные скобки в нём заменяются круглыми: номер в строке один.
    """
    if not sources:
        return ""
    body = "\n\n".join(_source_block(source) for source in sources)
    return f"<{_BLOCK}>\n{body}\n</{_BLOCK}>"


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
            fragment_id=fragment.fragment_id,
            quote=fragment.text,
        )
        context = knowledge_context([*sources, candidate])
        if estimator.text(rules) + estimator.text(context) > budget:
            break
        sources.append(candidate)
    return sources
