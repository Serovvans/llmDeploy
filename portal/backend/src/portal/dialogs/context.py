"""Сборка запроса к модели и усечение истории (docs/portal-api.md §5.4); чистые функции."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from portal.dialogs.domain import Attachment, Message
from portal.files.ports import MediaType
from portal.llm.ports import MAX_IMAGES_PER_REQUEST, TokenEstimator

_ATTACHMENT = "вложение"


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


def data_block(tag: str, body: str, name: str | None = None) -> str:
    """Размеченный блок недоверенных данных для запроса к модели.

    Разметка блока — только наша: закрывающая пометка в содержимом и в имени
    обезвреживается в любом написании (с пробелами, в любом регистре), так что
    содержимое не может закрыть свой блок и выдать продолжение за указания.
    """
    closing = re.compile(rf"<\s*/\s*{re.escape(tag)}", re.IGNORECASE)

    def inert(text: str) -> str:
        return closing.sub(rf"<\\/{tag}", text)

    opening = tag
    if name is not None:
        shown = inert(name).replace('"', "'").replace("\n", " ")
        opening = f'{tag} имя="{shown}"'
    return f"<{opening}>\n{inert(body)}\n</{tag}>"


def _attachment_block(attachment: Attachment) -> str:
    scans = len(attachment.image_pages)
    if attachment.text_content:
        body = attachment.text_content
    elif scans:
        body = f"(изображений: {scans}, они приложены к сообщению)"
    else:
        body = "(в файле нет текста)"
    return data_block(_ATTACHMENT, body, attachment.file_name)


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
    # Суммы по оставшейся истории ведутся вычитанием: проход по истории один.
    tokens = _tokens(history[dropped:], estimator)
    images = len(question.images) + sum(len(turn.images) for turn in history[dropped:])
    while dropped < len(history):
        fits = fixed + tokens <= budget and images <= MAX_IMAGES_PER_REQUEST
        if fits and history[dropped].role == "user":
            break
        tokens -= _tokens([history[dropped]], estimator)
        images -= len(history[dropped].images)
        dropped += 1
    return dropped
