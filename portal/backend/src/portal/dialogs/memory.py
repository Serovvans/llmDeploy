"""Оценка заполненности памяти диалога (docs/portal-api.md §5.10.3); чистые функции.

Оценка укладывает историю той же функцией `fit_memory`, что и запрос к модели, поэтому
уровень «переполнена» означает ровно то, что следующий запрос отбросит начало истории.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Literal
from uuid import UUID

from portal.dialogs.context import Turn, fit_memory, pair_turns, turns_tokens
from portal.dialogs.domain import Attachment, MemoryMode, Message
from portal.llm.ports import TokenEstimator

MemoryLevel = Literal["free", "filling", "almost_full", "overflow"]

_PREVIEW_CHARS = 200


@dataclass(frozen=True)
class PairEstimate:
    """Пара «вопрос + ответ» в оценке: что она добавляет в запрос и поместится ли."""

    question_id: UUID
    answer_id: UUID
    question_preview: str
    answer_preview: str
    attachments: int
    memory_mode: MemoryMode
    tokens: int
    dropped: bool


@dataclass(frozen=True)
class MemoryEstimate:
    """Объект `Memory` (§5.10.3)."""

    level: MemoryLevel
    percent: int
    used_tokens: int
    limit_tokens: int
    pairs: list[PairEstimate]


def estimate_memory(
    messages: Sequence[Message],
    attachments: Mapping[UUID, Sequence[Attachment]],
    system: str,
    question: Turn,
    budget: int,
    estimator: TokenEstimator,
) -> MemoryEstimate:
    """Оценить память диалога для следующего вопроса.

    `messages` — все сообщения диалога по порядку; `system`, `question` и `budget` — из
    плана следующего вопроса без текста и вложений, бюджет — за вычетом резерва базы
    знаний, если поиск будет выполняться (§5.10.2).
    """
    turns = pair_turns(messages, attachments)
    fit = fit_memory(system, [turn for _, turn in turns], question, budget, estimator)
    dropped = {pair for pair, _ in turns[: fit.dropped]}
    tokens: dict[UUID, int] = {}
    for pair, turn in turns:
        tokens[pair] = tokens.get(pair, 0) + turns_tokens([turn], estimator)
    pairs = [
        PairEstimate(
            question_id=asked.id,
            answer_id=answer.id,
            question_preview=asked.content[:_PREVIEW_CHARS],
            answer_preview=answer.content[:_PREVIEW_CHARS],
            attachments=len(attachments.get(asked.id, ())),
            memory_mode=asked.memory_mode,
            tokens=tokens.get(asked.id, 0),
            dropped=asked.id in dropped,
        )
        for asked, answer in pairwise(messages)
        if asked.role == "user" and answer.role == "assistant"
    ]
    if fit.dropped:
        level: MemoryLevel = "overflow"
    elif fit.used * 2 < fit.limit:
        level = "free"
    elif fit.used * 5 < fit.limit * 4:
        level = "filling"
    else:
        level = "almost_full"
    percent = 100 if fit.limit <= 0 else fit.used * 100 // fit.limit
    return MemoryEstimate(level, percent, fit.used, fit.limit, pairs)
