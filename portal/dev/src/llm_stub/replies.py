"""Что отвечает заглушка: чистые функции без состояния, случайности и времени."""

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

REASONING = "Размышление заглушки."
RECOGNITION_PROMPT = "Распознай текст страницы."
EMBEDDING_DIMENSION = 1024
EMBEDDING_MAX_WORDS = 8192
MAX_IMAGES = 8
SLOW_PARTS = 100
_WORDS_PER_PART = 3
_QUESTION_CHARS = 200
_REPLY_MARK = re.compile(r"\[\[stub:reply\]\](.*?)\[\[/stub:reply\]\]", re.DOTALL)
# Протоколы 0.15 (docs/portal-api.md §12.1): запрос узнаётся по первой строке сообщения.
REWRITE_PROMPT = "Перепиши вопрос для поиска."
IMAGE_PROMPT = "Опиши изображение из документа."
CONTEXT_PROMPT = "Поясни место фрагмента в документе."
SQL_DATA_PROMPT = "Подготовь тестовые данные."
SQL_FIX_PROMPT = "Исправь запрос."
RERANK_ERROR_MARK = "[[stub:rerank-error]]"
_SQL_DATA_MARK = re.compile(r"\[\[stub:sql-data\]\](.*?)\[\[/stub:sql-data\]\]", re.DOTALL)
_SQL_FIX_MARK = re.compile(r"\[\[stub:sql-fix\]\](.*?)\[\[/stub:sql-fix\]\]", re.DOTALL)
_NO_SQL_DATA = '{"statements": []}'
_WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class UserTurn:
    """Последнее сообщение пользователя: его текст и изображения."""

    text: str
    images: Sequence[bytes]


def _image_bytes(part: Mapping[str, Any]) -> bytes:
    url = str(part.get("image_url", {}).get("url", ""))
    return base64.b64decode(url.partition(",")[2])


def message_images(message: Mapping[str, Any]) -> list[bytes]:
    """Изображения одного сообщения."""
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [_image_bytes(part) for part in content if part.get("type") == "image_url"]


def last_user_turn(messages: Sequence[Mapping[str, Any]]) -> UserTurn | None:
    """Текст и изображения последнего сообщения пользователя; `None` — такого нет."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return UserTurn(content, [])
        parts = content if isinstance(content, list) else []
        text = "".join(str(part.get("text", "")) for part in parts if part.get("type") == "text")
        return UserTurn(text, message_images(message))
    return None


def schema_instance(schema: Mapping[str, Any], name: str = "") -> Any:
    """Значение, валидное по JSON-схеме (правило 3 контракта)."""
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = kind[0]
    if kind == "object":
        properties: Mapping[str, Any] = schema.get("properties", {})
        return {key: schema_instance(value, key) for key, value in properties.items()}
    if kind == "array":
        items: Mapping[str, Any] = schema.get("items", {})
        return [schema_instance(items, name) for _ in range(schema.get("minItems", 1))]
    if kind in ("number", "integer"):
        return 0
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return f"заглушка: {name}"


def _block(text: str, tag: str) -> str:
    """Текст последнего закрытого блока `<tag>…</tag>` без пробелов по краям; нет блока — пусто."""
    found = re.search(rf".*<{tag}>(.*?)</{tag}>", text, re.DOTALL)
    return found.group(1).strip() if found else ""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:8]


def protocol_reply(turn: UserTurn) -> str | None:
    """Ответ на запрос одного из протоколов 0.15; `None` — это не такой запрос.

    Проверяется раньше остальных правил: пометка `[[stub:reply]]` из вопроса теста,
    попавшего в блок запроса, его ответ не подменяет.
    """
    first_line = turn.text.partition("\n")[0]
    if first_line == REWRITE_PROMPT:
        asked = _block(_block(turn.text, "диалог"), "пользователь")
        return " ".join(part for part in (asked, _block(turn.text, "вопрос")) if part)
    if first_line == IMAGE_PROMPT and len(turn.images) == 1:
        return f"Описание изображения-заглушки {_digest(turn.images[0])}."
    if first_line == CONTEXT_PROMPT:
        return f"Контекстная справка {_digest(_block(turn.text, 'фрагмент').encode())}."
    if first_line == SQL_DATA_PROMPT:
        marked = _SQL_DATA_MARK.search(turn.text)
        return marked.group(1) if marked else _NO_SQL_DATA
    if first_line == SQL_FIX_PROMPT:
        marked = _SQL_FIX_MARK.search(turn.text)
        return marked.group(1) if marked else f"```sql\n{_block(turn.text, 'запрос')}\n```"
    return None


def reply_text(
    turn: UserTurn, response_format: Mapping[str, Any] | None, images_in_request: int
) -> str:
    """Текст ответа: протоколы 0.15, затем первое подходящее правило контракта, сверху вниз."""
    if (by_protocol := protocol_reply(turn)) is not None:
        return by_protocol
    if (marked := _REPLY_MARK.search(turn.text)) is not None:
        return marked.group(1)
    if turn.text == RECOGNITION_PROMPT and len(turn.images) == 1:
        return f"Текст страницы-заглушки {_digest(turn.images[0])}."
    if response_format is not None and response_format.get("type") == "json_schema":
        schema = response_format.get("json_schema", {}).get("schema", {})
        return json.dumps(schema_instance(schema), ensure_ascii=False)
    answer = f"Заглушка. Вопрос: «{turn.text[:_QUESTION_CHARS]}». Изображений: {images_in_request}."
    if any(line.startswith("[1] ") for line in turn.text.splitlines()):
        answer += " [1]"
    return answer


def text_parts(text: str, *, slow: bool) -> list[str]:
    """Части ответа по несколько слов; их конкатенация равна тексту.

    С пометкой `[[stub:slow]]` частей ровно 100: текст повторяется по кругу.
    """
    tokens = re.findall(r"^\s+|\S+\s*", text)
    parts = [
        "".join(tokens[start : start + _WORDS_PER_PART])
        for start in range(0, len(tokens), _WORDS_PER_PART)
    ]
    if not slow:
        return parts
    parts = parts or [" "]
    return [parts[index % len(parts)] for index in range(SLOW_PARTS)]


def embedding(text: str) -> list[float] | None:
    """Вектор по хешам слов, нормированный по L2; `None` — текст длиннее лимита."""
    words = re.findall(r"\w+", text.lower())
    if len(words) > EMBEDDING_MAX_WORDS:
        return None
    vector = [0.0] * EMBEDDING_DIMENSION
    for word in words:
        digest = hashlib.sha256(word.encode()).digest()
        vector[int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSION] += 1.0
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


def rerank(query: str, documents: Sequence[str]) -> list[dict[str, Any]]:
    """Оценки реранкера: доля слов запроса, которые есть в документе.

    Слова — отрезки из букв и цифр в нижнем регистре; запрос без слов — оценка 0.
    Записи идут по убыванию оценки, при равенстве — по возрастанию `index`.
    """
    wanted = set(_WORD.findall(query.lower()))
    scores = [
        len(wanted & set(_WORD.findall(document.lower()))) / len(wanted) if wanted else 0.0
        for document in documents
    ]
    order = sorted(range(len(documents)), key=lambda index: (-scores[index], index))
    return [{"index": index, "relevance_score": scores[index]} for index in order]
