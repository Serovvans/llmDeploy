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


def reply_text(
    turn: UserTurn, response_format: Mapping[str, Any] | None, images_in_request: int
) -> str:
    """Текст ответа: первое подходящее правило контракта, сверху вниз."""
    if (marked := _REPLY_MARK.search(turn.text)) is not None:
        return marked.group(1)
    if turn.text == RECOGNITION_PROMPT and len(turn.images) == 1:
        digest = hashlib.sha256(turn.images[0]).hexdigest()[:8]
        return f"Текст страницы-заглушки {digest}."
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
