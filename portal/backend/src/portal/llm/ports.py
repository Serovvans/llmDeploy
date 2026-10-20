"""Порты обращения к модели (docs/portal-api.md §13.3); ими пользуются dialogs, tools, kb."""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

ReasoningEffort = Literal["low", "medium", "xhigh"]

# Лимит модели (docs/design.md §6), а не параметр портала.
MAX_IMAGES_PER_REQUEST = 8


@dataclass(frozen=True)
class TextPart:
    """Текстовая часть сообщения."""

    text: str


@dataclass(frozen=True)
class ImagePart:
    """Изображение в сообщении."""

    data: bytes
    media_type: Literal["image/png", "image/jpeg"]


@dataclass(frozen=True)
class ModelMessage:
    """Сообщение запроса к модели."""

    role: Literal["system", "user", "assistant"]
    parts: Sequence[TextPart | ImagePart]


@dataclass(frozen=True)
class ChatRequest:
    """Запрос к модели: только то, что выражается стандартными полями OpenAI API.

    `include_usage` — просить у шлюза фактический расход запроса в потоке (§12.1).
    """

    messages: Sequence[ModelMessage]
    reasoning_effort: ReasoningEffort
    max_tokens: int
    json_schema: Mapping[str, Any] | None = None
    include_usage: bool = False


@dataclass(frozen=True)
class ReasoningDelta:
    """Очередная часть размышлений."""

    text: str


@dataclass(frozen=True)
class ContentDelta:
    """Очередная часть ответа."""

    text: str


@dataclass(frozen=True)
class Usage:
    """Фактическое число токенов запроса по данным шлюза (§5.10.10)."""

    prompt_tokens: int


@dataclass(frozen=True)
class Finished:
    """Модель завершила ответ; без этого события ответ полным не считается."""

    reason: Literal["stop", "length"]


class ChatModel(Protocol):
    """Модель чата."""

    def stream(
        self, request: ChatRequest
    ) -> AsyncIterator[ReasoningDelta | ContentDelta | Usage | Finished]:
        """Потоковый ответ; последним приходит `Finished`.

        `Usage` — не больше одного раза, только при `include_usage` и только прямо перед
        `Finished`; читающий поток отличает его от `ContentDelta` явно.
        """
        ...

    async def complete(self, request: ChatRequest) -> str:
        """Ответ целиком, без потока."""
        ...


@dataclass(frozen=True)
class Completion:
    """Ответ без потока с причиной окончания (docs/portal-api.md §12.1)."""

    text: str
    finish_reason: Literal["stop", "length"]


class CompletionModel(Protocol):
    """Модель, отвечающая без потока с причиной окончания; нужна описанию картинок.

    Отдельный порт, а не метод `ChatModel`: подмены `ChatModel` в тестах не меняются.
    """

    async def complete_with_reason(self, request: ChatRequest) -> Completion:
        """Ответ целиком; исключения — как у `ChatModel.complete`."""
        ...


class TokenEstimator(Protocol):
    """Оценка числа токенов: точного счёта у портала нет."""

    def text(self, text: str) -> int:
        """Оценка для текста."""
        ...

    def image(self) -> int:
        """Оценка для одного изображения."""
        ...


class ModelUnavailableError(Exception):
    """Bifrost или модель не отвечает, вернула ошибку или оборвала поток."""


class ModelOverloadedError(Exception):
    """Лимит ключа портала исчерпан или модель не начала отвечать вовремя."""


class ContextOverflowError(Exception):
    """Модель отвергла запрос как слишком длинный."""
