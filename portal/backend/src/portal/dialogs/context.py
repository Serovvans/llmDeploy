"""Сборка запроса к модели и укладка истории (docs/portal-api.md §5.4, §5.10); чистые функции."""

import re
import sys
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any, Literal, cast
from uuid import UUID

from portal.dialogs.domain import Attachment, Dialog, Message
from portal.files.ports import MediaType
from portal.llm.ports import MAX_IMAGES_PER_REQUEST, TokenEstimator

_ATTACHMENT = "вложение"
CARRIED_TAG = "перенесённое"
SUMMARY_TAG = "пересказ"


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


@cache
def _gap() -> str:
    """Допуск между частями закрывающей пометки: пробелы и невидимые знаки формата (Cf).

    Модель Cf не видит, а пробельный класс их не ловит. Набор берётся из `unicodedata`
    текущей версии Python (список в коде устарел бы с её обновлением); считается один раз.
    """
    format_chars = "".join(
        re.escape(char)
        for char in map(chr, range(sys.maxunicode + 1))
        if unicodedata.category(char) == "Cf"
    )
    return rf"[\s{format_chars}]*"


@cache
def _closing(tag: str) -> re.Pattern[str]:
    return re.compile(rf"<{_gap()}/{_gap()}{re.escape(tag)}", re.IGNORECASE)


def data_block(tag: str, body: str, name: str | None = None) -> str:
    """Размеченный блок недоверенных данных для запроса к модели.

    Разметка блока — только наша: закрывающая пометка в содержимом и в имени
    обезвреживается в любом написании (с пробелами и невидимыми знаками формата, в любом
    регистре), так что содержимое не может закрыть свой блок и выдать продолжение за указания.
    """
    closing = _closing(tag)

    def inert(text: str) -> str:
        return closing.sub(rf"<\\/{tag}", text)

    opening = tag
    if name is not None:
        shown = inert(name).replace('"', "'").replace("\n", " ")
        opening = f'{tag} имя="{shown}"'
    return f"<{opening}>\n{inert(body)}\n</{tag}>"


def fields_block(fields: Sequence[Mapping[str, Any]]) -> str:
    """Блок с таблицей извлечённых реквизитов разбора документа."""
    lines = [f"{field['title']}: {field['value'] or '—'}" for field in fields]
    return data_block("реквизиты", "\n".join(lines) or "(реквизиты не извлечены)")


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
        # Страницы-сканы есть только у документов: у записи их не бывает.
        ImageRef(attachment.storage_key, cast(MediaType, attachment.media_type), page)
        for attachment in attachments
        for page in attachment.image_pages
    )
    return Turn("user", "\n\n".join([*blocks, content]).strip(), images)


def head_turn(dialog: Dialog) -> Turn | None:
    """Головное сообщение: перенесённое и пересказ блоками данных; `None` — нет ни того, ни другого.

    Пересказ и перенесённое — недоверенные данные: они идут только в сообщении роли
    `user`, никогда в системном сообщении (§5.10.1).
    """
    blocks = [
        data_block(tag, text)
        for tag, text in ((CARRIED_TAG, dialog.carried_text), (SUMMARY_TAG, dialog.summary))
        if text is not None
    ]
    return Turn("user", "\n\n".join(blocks)) if blocks else None


def with_head_rules(system: str, head: Turn | None, rules: str) -> str:
    """Системное сообщение с правилом о головном сообщении, если оно есть (§5.10.2)."""
    return f"{system}\n\n{rules.strip()}" if head is not None else system


def considered_history(
    messages: Sequence[Message], summarized_until: int | None = None
) -> list[tuple[Message, Message]]:
    """Сообщения учитываемой истории по порядку — каждое с вопросом своей пары (§5.10.1).

    Пара — вопрос и следующий за ним ответ; режим пары хранится у вопроса. Сообщения не
    дальше границы пересказа `summarized_until` и обе строки пар «Не учитывать» убраны;
    пустые ответы ещё есть — их пропускает тот, кто собирает текст.
    """
    considered: list[tuple[Message, Message]] = []
    question: Message | None = None
    for message in messages:
        if message.role == "user":
            question = message
        pair = question or message
        summarized = summarized_until is not None and message.position <= summarized_until
        if pair.memory_mode != "excluded" and not summarized:
            considered.append((message, pair))
    return considered


def pair_turns(
    messages: Sequence[Message],
    attachments: Mapping[UUID, Sequence[Attachment]],
    summarized_until: int | None = None,
) -> list[tuple[UUID, Turn]]:
    """Учитываемая история для модели — каждый ход с идентификатором вопроса своей пары.

    У вопроса пары «Без вложений» нет блоков и картинок вложений; ответ с пустым текстом
    пропускается, размышления не входят.
    """
    turns: list[tuple[UUID, Turn]] = []
    for message, question in considered_history(messages, summarized_until):
        if message.role == "user":
            files = [] if message.memory_mode == "text_only" else attachments.get(message.id, [])
            turns.append((question.id, question_turn(message.content, files)))
        elif message.content:
            turns.append((question.id, Turn("assistant", message.content)))
    return turns


def history_turns(
    messages: Sequence[Message],
    attachments: Mapping[UUID, Sequence[Attachment]],
    summarized_until: int | None = None,
) -> list[Turn]:
    """Учитываемая история по порядку (§5.10.1): то, что уходит модели перед вопросом."""
    return [turn for _, turn in pair_turns(messages, attachments, summarized_until)]


def turns_tokens(turns: Sequence[Turn], estimator: TokenEstimator) -> int:
    """Оценка ходов: текст и изображения."""
    return sum(estimator.text(turn.text) + estimator.image() * len(turn.images) for turn in turns)


@dataclass(frozen=True)
class MemoryFit:
    """Итог укладки истории в запрос (§5.10.2).

    `dropped` — сколько сообщений истории отбросить с начала; `used` — оценка головного
    сообщения и всей переданной истории до отбрасывания; `limit` — объём под разговор:
    бюджет минус системное сообщение и текущий вопрос.
    """

    dropped: int
    used: int
    limit: int


def fit_memory(
    system: str,
    history: Sequence[Turn],
    question: Turn,
    budget: int,
    estimator: TokenEstimator,
    min_dropped: int = 0,
    *,
    head: Turn | None = None,
) -> MemoryFit:
    """Уложить историю в бюджет — одна функция для запроса к модели и для оценки памяти.

    История отбрасывается целыми сообщениями с начала, пока она вместе с головным
    сообщением `head` не уложится в объём под разговор, картинок в запросе не станет не
    больше 8 и оставшаяся история не начнётся с вопроса. Головное сообщение не
    отбрасывается никогда. `min_dropped` — отбросить не меньше стольких (повтор после
    отказа модели).
    """
    limit = budget - estimator.text(system) - turns_tokens([question], estimator)
    used = turns_tokens([] if head is None else [head], estimator) + turns_tokens(
        history, estimator
    )
    dropped = min(min_dropped, len(history))
    # Суммы по оставшейся истории ведутся вычитанием: проход по истории один.
    tokens = used - turns_tokens(history[:dropped], estimator)
    images = len(question.images) + sum(len(turn.images) for turn in history[dropped:])
    while dropped < len(history):
        fits = tokens <= limit and images <= MAX_IMAGES_PER_REQUEST
        if fits and history[dropped].role == "user":
            break
        tokens -= turns_tokens([history[dropped]], estimator)
        images -= len(history[dropped].images)
        dropped += 1
    return MemoryFit(dropped, used, limit)
