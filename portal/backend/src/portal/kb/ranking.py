"""Порядок выдачи: гарантированные лексические места и реранкер (docs/portal-api.md §8.4, §10.4)."""

import logging
from collections.abc import Mapping, Sequence
from uuid import UUID

from portal.kb.ports import Reranker, RerankUnavailableError

logger = logging.getLogger(__name__)


async def ranked(
    lexical: Sequence[UUID],
    hybrid: Sequence[UUID],
    texts: Mapping[UUID, str],
    query: str,
    reranker: Reranker | None,
    top_k: int,
) -> list[UUID]:
    """Выдача: лучшие лексические совпадения в их порядке, затем остальные кандидаты.

    `texts` — тексты для поиска кандидатов, прошедших вторую проверку: кандидат, которого
    в них нет, отбрасывается. Остальных переупорядочивает реранкер; `None` (флаг выключен)
    или сбой реранкера — порядок слияния ветвей, без ошибки. Лексические места от оценки
    реранкера не зависят.
    """
    first = [found for found in dict.fromkeys(lexical) if found in texts]
    rest = [found for found in dict.fromkeys(hybrid) if found in texts and found not in first]
    if reranker is not None and len(rest) > 1:
        try:
            scores = await reranker.scores(query, [texts[found] for found in rest])
        except RerankUnavailableError:
            logger.warning("reranker unavailable", extra={"candidates": len(rest)})
        else:
            by_score = sorted(zip(scores, range(len(rest)), strict=True), key=lambda s: -s[0])
            rest = [rest[position] for _, position in by_score]
    return [*first, *rest][:top_k]
