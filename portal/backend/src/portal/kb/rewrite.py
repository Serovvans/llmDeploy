"""Переписывание уточняющего вопроса в запрос для поиска (docs/portal-api.md §8.4, §12.1).

Переписанный текст служит только поиску. Сбой, пустой ответ и превышение срока ошибкой
не считаются: поиск идёт по исходному вопросу. Тексты в журнал не попадают.
"""

import asyncio
import logging
from collections.abc import Sequence

from portal.core.settings import KbRewriteSettings
from portal.kb import markup
from portal.kb.ports import DialogTurn
from portal.llm.ports import ChatModel, ChatRequest, ModelMessage, TextPart

logger = logging.getLogger(__name__)

# Первая строка запроса зафиксирована контрактом: по ней запрос узнаёт заглушка стенда.
_INSTRUCTION = "Перепиши вопрос для поиска."
_TAGS = {"user": markup.USER, "assistant": markup.ASSISTANT}
_QUOTES = "\"'«»“”„"


def _recent(history: Sequence[DialogTurn], messages: int, max_chars: int) -> list[DialogTurn]:
    """Последние сообщения истории по порядку: не больше `messages` и `max_chars` символов.

    Счёт идёт от самого свежего; сообщение, которое не помещается целиком, обрезается.
    """
    recent: list[DialogTurn] = []
    left = max_chars
    for turn in reversed(history[-messages:]):
        if left <= 0:
            break
        recent.append(DialogTurn(turn.role, turn.text[:left]))
        left -= len(turn.text)
    return recent[::-1]


def _first_line(answer: str) -> str:
    """Первая непустая строка ответа без кавычек по краям; пусто — переписывания нет."""
    for line in answer.splitlines():
        if cleaned := line.strip().strip(_QUOTES).strip():
            return cleaned
    return ""


class QueryRewriter:
    """Одно короткое обращение к модели перед поиском."""

    def __init__(self, model: ChatModel, settings: KbRewriteSettings) -> None:
        """Получить модель и параметры переписывания."""
        self._model = model
        self._settings = settings

    async def rewrite(self, question: str, history: Sequence[DialogTurn]) -> str:
        """Самостоятельный запрос по вопросу и истории; при любом сбое — сам вопрос.

        Для первого сообщения диалога (история пуста) и при выключенном флаге модель не
        вызывается. Отмена задачи не перехватывается.
        """
        if not self._settings.enabled or not history:
            return question
        try:
            async with asyncio.timeout(self._settings.timeout_seconds):
                answer = await self._model.complete(self._request(question, history))
        except Exception as error:
            # В тексте исключения может оказаться текст запроса: в журнал — только тип.
            logger.warning("query rewrite failed", extra={"error_type": type(error).__name__})
            return question
        return _first_line(answer) or question

    def _request(self, question: str, history: Sequence[DialogTurn]) -> ChatRequest:
        settings = self._settings
        turns = "\n".join(
            f"<{_TAGS[turn.role]}>{markup.inert(turn.text)}</{_TAGS[turn.role]}>"
            for turn in _recent(history, settings.history_messages, settings.history_max_chars)
        )
        text = "\n".join(
            [
                _INSTRUCTION,
                f"<{markup.DIALOG}>\n{turns}\n</{markup.DIALOG}>",
                markup.block(markup.QUESTION, question),
            ]
        )
        return ChatRequest(
            messages=(
                ModelMessage("system", (TextPart(settings.system_prompt),)),
                ModelMessage("user", (TextPart(text),)),
            ),
            reasoning_effort="low",
            max_tokens=settings.max_tokens,
        )
