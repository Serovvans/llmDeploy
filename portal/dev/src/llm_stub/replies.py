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
# Протокол «Пересказ» 0.8 (§12.1): черновик сжатия и переноса памяти диалога.
SUMMARY_PROMPT = "Перескажи диалог."
CODE_FIX_PROMPT = "Исправь ошибки сборки."
CODE_EDIT_LINE = "// Правка заглушки."
_CODE_FIX_MARK = re.compile(r"\[\[stub:code-fix\]\](.*?)\[\[/stub:code-fix\]\]", re.DOTALL)
_PROJECT_LINE = re.compile(r"^<проект ", re.MULTILINE)
_PROJECT_FILE = re.compile(r'<файл имя="([^"\n]*)">\n(.*?)\n</файл>', re.DOTALL)
_SUMMARY_MESSAGE = re.compile(r"<(?:пользователь|помощник)>")
RERANK_ERROR_MARK = "[[stub:rerank-error]]"
NO_USAGE_MARK = "[[stub:no-usage]]"
# Расход в фрагменте `usage` (§12.2): намеренно не совпадает с оценкой портала.
_USAGE_CHARS_PER_TOKEN = 3
_USAGE_TOKENS_PER_IMAGE = 1500
_SQL_DATA_MARK = re.compile(r"\[\[stub:sql-data\]\](.*?)\[\[/stub:sql-data\]\]", re.DOTALL)
_SQL_FIX_MARK = re.compile(r"\[\[stub:sql-fix\]\](.*?)\[\[/stub:sql-fix\]\]", re.DOTALL)
_NO_SQL_DATA = '{"statements": []}'
_WORD = re.compile(r"[^\W_]+")
# Особые ширины PNG в запросе описания картинки (§12.2): проверка правил описания §8.6.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_EMPTY_WIDTH = 701
_LONG_WIDTH = 702
_CUT_WIDTH = 703
_LONG_DESCRIPTION_CHARS = 3000
_FULL_DESCRIPTION_TOKENS = 4096


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


def _text_chars(message: Mapping[str, Any]) -> int:
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    parts = content if isinstance(content, list) else []
    return sum(len(str(part.get("text", ""))) for part in parts if part.get("type") == "text")


def usage(messages: Sequence[Mapping[str, Any]], images: int, answer: str) -> dict[str, int]:
    """Объект `usage` фрагмента потока (§12.2).

    `prompt_tokens` — символы всех текстовых частей всех сообщений, делённые на 3 с
    округлением вверх, плюс 1500 на изображение; `completion_tokens` — символы ответа на 3.
    """
    chars = sum(_text_chars(message) for message in messages)
    prompt = math.ceil(chars / _USAGE_CHARS_PER_TOKEN) + _USAGE_TOKENS_PER_IMAGE * images
    completion = math.ceil(len(answer) / _USAGE_CHARS_PER_TOKEN)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


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


def _png_width(data: bytes) -> int | None:
    """Ширина PNG из заголовка `IHDR`; `None` — это не PNG."""
    if not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big")


def image_description(image: bytes, max_tokens: int | None) -> tuple[str, str]:
    """Описание картинки и `finish_reason`; особые ширины PNG 701–703 (§12.2).

    701 — пустой ответ; 702 — описание длиннее 3000 символов; 703 — при `max_tokens`
    меньше 4096 начало описания с `length`, иначе обычный ответ. Без `max_tokens` предела
    нет — обычный ответ.
    """
    digest = _digest(image)
    width = _png_width(image)
    if width == _EMPTY_WIDTH:
        return "", "stop"
    if width == _LONG_WIDTH:
        text = ""
        number = 0
        while len(text) <= _LONG_DESCRIPTION_CHARS:
            number += 1
            line = f"Описание изображения-заглушки {digest}, строка {number}."
            text = f"{text}\n{line}" if text else line
        return text, "stop"
    if width == _CUT_WIDTH and max_tokens is not None and max_tokens < _FULL_DESCRIPTION_TOKENS:
        return f"Начало описания-заглушки {digest}.", "length"
    return f"Описание изображения-заглушки {digest}.", "stop"


def _first_file(text: str) -> tuple[str, str] | None:
    """Путь и содержимое первого файла блока `<проект>` (§7.5.9); `None` — файлов нет."""
    start = _PROJECT_LINE.search(text)
    found = _PROJECT_FILE.search(text, start.start()) if start else None
    return (found.group(1), found.group(2)) if found else None


def _edit_block(path: str, content: str) -> str:
    """Блок правки «файл целиком» (§7.5.10)."""
    return f"```csharp path={path}\n{content}\n```"


def code_edit(text: str) -> str:
    """Правило 2а: правка первого файла проекта со строкой заглушки в конце (§12.2)."""
    first = _first_file(text)
    path, content = first if first else ("StubFile.cs", "// Файл заглушки.")
    if first:
        content = f"{content.rstrip(chr(10))}\n{CODE_EDIT_LINE}"
    head = "Правка-заглушка."
    if any(line.startswith("[1] ") for line in text.splitlines()):
        head += " [1]"
    return f"{head}\n\n{_edit_block(path, content)}"


def protocol_reply(turn: UserTurn, max_tokens: int | None = None) -> str | None:
    """Ответ на запрос одного из протоколов 0.15 или пересказа 0.8; `None` — не такой запрос.

    Проверяется раньше остальных правил: пометка `[[stub:reply]]` из вопроса теста,
    попавшего в блок запроса, его ответ не подменяет.
    """
    first_line = turn.text.partition("\n")[0]
    if first_line == REWRITE_PROMPT:
        asked = _block(_block(turn.text, "диалог"), "пользователь")
        return " ".join(part for part in (asked, _block(turn.text, "вопрос")) if part)
    if first_line == IMAGE_PROMPT and len(turn.images) == 1:
        return image_description(turn.images[0], max_tokens)[0]
    if first_line == CONTEXT_PROMPT:
        return f"Контекстная справка {_digest(_block(turn.text, 'фрагмент').encode())}."
    if first_line == SQL_DATA_PROMPT:
        marked = _SQL_DATA_MARK.search(turn.text)
        return marked.group(1) if marked else _NO_SQL_DATA
    if first_line == SUMMARY_PROMPT:
        messages = len(_SUMMARY_MESSAGE.findall(turn.text))
        return f"Пересказ-заглушка {_digest(turn.text.encode())}: сообщений {messages}."
    if first_line == SQL_FIX_PROMPT:
        marked = _SQL_FIX_MARK.search(turn.text)
        return marked.group(1) if marked else f"```sql\n{_block(turn.text, 'запрос')}\n```"
    if first_line == CODE_FIX_PROMPT:
        # Без пометки — первый файл без изменений: правка пуста, цикл кончается (§7.5.11).
        marked = _CODE_FIX_MARK.search(turn.text)
        first = _first_file(turn.text)
        unchanged = _edit_block(*first) if first else ""
        return marked.group(1) if marked else unchanged
    return None


def reply_text(
    turn: UserTurn,
    response_format: Mapping[str, Any] | None,
    images_in_request: int,
    max_tokens: int | None = None,
) -> str:
    """Текст ответа: протоколы 0.15, затем первое подходящее правило контракта, сверху вниз."""
    if (by_protocol := protocol_reply(turn, max_tokens)) is not None:
        return by_protocol
    if (marked := _REPLY_MARK.search(turn.text)) is not None:
        return marked.group(1)
    if turn.text == RECOGNITION_PROMPT and len(turn.images) == 1:
        return f"Текст страницы-заглушки {_digest(turn.images[0])}."
    if _PROJECT_LINE.search(turn.text):
        return code_edit(turn.text)
    if response_format is not None and response_format.get("type") == "json_schema":
        schema = response_format.get("json_schema", {}).get("schema", {})
        return json.dumps(schema_instance(schema), ensure_ascii=False)
    answer = f"Заглушка. Вопрос: «{turn.text[:_QUESTION_CHARS]}». Изображений: {images_in_request}."
    if any(line.startswith("[1] ") for line in turn.text.splitlines()):
        answer += " [1]"
    return answer


def finish_reason(turn: UserTurn, max_tokens: int | None) -> str:
    """`length` — пометка `[[stub:length]]` или описание картинки шириной 703 до 4096 токенов."""
    if "[[stub:length]]" in turn.text:
        return "length"
    if turn.text.partition("\n")[0] == IMAGE_PROMPT and len(turn.images) == 1:
        return image_description(turn.images[0], max_tokens)[1]
    return "stop"


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
