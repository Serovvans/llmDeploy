"""Сборка запроса к модели и усечение истории (docs/portal-api.md §5.4); чистые функции."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from portal.dialogs.domain import Attachment, Message
from portal.files.ports import MediaType
from portal.llm.ports import MAX_IMAGES_PER_REQUEST, TokenEstimator

_BLOCK = "вложение"
# Закрывающая пометка блока в любом написании: с пробелами и в любом регистре.
_CLOSING_TAG = re.compile(rf"<\s*/\s*{_BLOCK}", re.IGNORECASE)


@dataclass(frozen=True)
class ImageRef:
    """Изображение, которое подгружается перед отправкой: файл и номер страницы."""

    storage_key: str
    media_type: MediaType
    page: int


@dataclass(frozen=True)
class Turn:
    """Сообщение запроса к модели до загрузки изображений."""

    role: Literal["user", "assistant"]
    text: str
    images: tuple[ImageRef, ...] = ()


def _inert(text: str) -> str:
    """Не дать содержимому закрыть свой блок данных: разметка блока — только наша."""
    return _CLOSING_TAG.sub(rf"<\\/{_BLOCK}", text)


def _attachment_block(attachment: Attachment) -> str:
    name = _inert(attachment.file_name).replace('"', "'").replace("\n", " ")
    scans = len(attachment.image_pages)
    if attachment.text_content:
        body = _inert(attachment.text_content)
    elif scans:
        body = f"(изображений: {scans}, они приложены к сообщению)"
    else:
        body = "(в файле нет текста)"
    return f'<{_BLOCK} имя="{name}">\n{body}\n</{_BLOCK}>'


def question_turn(content: str, attachments: Sequence[Attachment]) -> Turn:
    """Вопрос пользователя: блоки данных вложений, затем его текст; сканы — картинками.

    Имена и содержимое файлов — недоверенные данные: они идут в сообщении пользователя в
    размеченных блоках и никогда — в системном сообщении.
    """
    blocks = [_attachment_block(attachment) for attachment in attachments]
    images = tuple(
        ImageRef(attachment.storage_key, attachment.media_type, page)
        for attachment in attachments
        for page in attachment.image_pages
    )
    return Turn("user", "\n\n".join([*blocks, content]).strip(), images)


def history_turns(
    messages: Sequence[Message], attachments: Mapping[UUID, Sequence[Attachment]]
) -> list[Turn]:
    """История по порядку; ответ с пустым текстом пропускается, размышления не входят."""
    turns: list[Turn] = []
    for message in messages:
        if message.role == "user":
            turns.append(question_turn(message.content, attachments.get(message.id, [])))
        elif message.content:
            turns.append(Turn("assistant", message.content))
    return turns


def _tokens(turns: Sequence[Turn], estimator: TokenEstimator) -> int:
    return sum(estimator.text(turn.text) + estimator.image() * len(turn.images) for turn in turns)


def fits_alone(system: str, question: Turn, budget: int, estimator: TokenEstimator) -> bool:
    """Помещаются ли в бюджет системное сообщение и текущий вопрос без истории."""
    return estimator.text(system) + _tokens([question], estimator) <= budget


def fit_history(
    system: str,
    history: Sequence[Turn],
    question: Turn,
    budget: int,
    estimator: TokenEstimator,
    min_dropped: int = 0,
) -> int:
    """Сколько ранних сообщений истории отбросить.

    История отбрасывается целыми сообщениями с начала, пока запрос не уложится в бюджет
    и в нём не останется не больше 8 изображений; оставшаяся начинается с вопроса.
    """
    fixed = estimator.text(system) + _tokens([question], estimator)
    dropped = min(min_dropped, len(history))
    while dropped < len(history):
        kept = history[dropped:]
        images = len(question.images) + sum(len(turn.images) for turn in kept)
        fits = fixed + _tokens(kept, estimator) <= budget and images <= MAX_IMAGES_PER_REQUEST
        if fits and kept[0].role == "user":
            break
        dropped += 1
    return dropped
