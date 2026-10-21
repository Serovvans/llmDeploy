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
    summarized: bool


@dataclass(frozen=True)
class PairRange:
    """Номера пар с 1, как в `Memory.pairs`: от `first` до `last` включительно."""

    first: int
    last: int


@dataclass(frozen=True)
class MemoryEstimate:
    """Объект `Memory` (§5.10.3)."""

    level: MemoryLevel
    percent: int
    used_tokens: int
    limit_tokens: int
    compress_pairs: PairRange | None
    pairs: list[PairEstimate]


def dialog_pairs(messages: Sequence[Message]) -> list[tuple[Message, Message]]:
    """Пары «вопрос + ответ» диалога по возрастанию позиции (§5.10.1)."""
    return [
        (asked, answer)
        for asked, answer in pairwise(messages)
        if asked.role == "user" and answer.role == "assistant"
    ]


def compress_range(
    pairs: Sequence[tuple[Message, Message]], summarized_until: int | None, keep_recent: int
) -> PairRange | None:
    """Пары, которые следующее сжатие поставит за границу пересказа (§5.10.5).

    От первой пары после прежней границы до пары перед первой из последних `keep_recent`
    пар не в режиме «Не учитывать». `None` — сжимать нечего: в диапазоне нет ни одной
    учитываемой пары.
    """
    kept = 0
    first_kept = len(pairs)
    while first_kept > 0 and kept < keep_recent:
        first_kept -= 1
        if pairs[first_kept][0].memory_mode != "excluded":
            kept += 1
    if kept < keep_recent:
        return None
    start = 0
    if summarized_until is not None:
        start = next(
            (n for n, (_, answer) in enumerate(pairs) if answer.position > summarized_until),
            len(pairs),
        )
    chosen = pairs[start:first_kept]
    if not any(asked.memory_mode != "excluded" for asked, _ in chosen):
        return None
    return PairRange(start + 1, first_kept)


def estimate_memory(
    messages: Sequence[Message],
    attachments: Mapping[UUID, Sequence[Attachment]],
    system: str,
    head: Turn | None,
    summarized_until: int | None,
    question: Turn,
    budget: int,
    estimator: TokenEstimator,
    keep_recent: int,
) -> MemoryEstimate:
    """Оценить память диалога для следующего вопроса.

    `messages` — все сообщения диалога по порядку; `system`, `question` и `budget` — из
    плана следующего вопроса без текста и вложений, бюджет — за вычетом резерва базы
    знаний, если поиск будет выполняться (§5.10.2); `system` — уже с правилом головного
    сообщения `head`, если оно есть. `summarized_until` — граница пересказа.
    """
    turns = pair_turns(messages, attachments, summarized_until)
    history = [turn for _, turn in turns]
    fit = fit_memory(system, history, question, budget, estimator, head=head)
    dropped = {pair for pair, _ in turns[: fit.dropped]}
    tokens: dict[UUID, int] = {}
    pairs = dialog_pairs(messages)
    for pair, turn in turns:
        tokens[pair] = tokens.get(pair, 0) + turns_tokens([turn], estimator)
    estimates = [
        PairEstimate(
            question_id=asked.id,
            answer_id=answer.id,
            question_preview=asked.content[:_PREVIEW_CHARS],
            answer_preview=answer.content[:_PREVIEW_CHARS],
            attachments=len(attachments.get(asked.id, ())),
            memory_mode=asked.memory_mode,
            tokens=tokens.get(asked.id, 0),
            dropped=asked.id in dropped,
            summarized=summarized_until is not None and answer.position <= summarized_until,
        )
        for asked, answer in pairs
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
    return MemoryEstimate(
        level,
        percent,
        fit.used,
        fit.limit,
        compress_range(pairs, summarized_until, keep_recent),
        estimates,
    )
