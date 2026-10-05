"""Структурированные журналы в stdout: одна строка JSON на запись.

В записи попадают только поля, переданные явно через `extra`; тела запросов, пароли,
значения cookie и тексты сообщений сюда не передаются (docs/portal-api.md §13.5).
"""

import json
import logging
import sys
import traceback
from datetime import UTC, datetime

# color_message — дубль сообщения uvicorn с управляющими символами терминала.
_SKIPPED_ATTRIBUTES = frozenset(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
    "color_message",
}


class JsonFormatter(logging.Formatter):
    """Форматирует запись журнала как объект JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Собрать строку JSON из записи и её дополнительных полей."""
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _SKIPPED_ATTRIBUTES:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging() -> None:
    """Направить все журналы процесса в stdout в виде JSON."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


def code_locations(error: BaseException) -> list[str]:
    """Места в коде, через которые прошло исключение, без его текста и без значений.

    В тексте исключения и в стандартной трассировке (она его включает) могут оказаться
    данные запроса, поэтому в журнал идут только файл, строка и функция.
    """
    return [
        f"{frame.filename}:{frame.lineno} {frame.name}"
        for frame in traceback.extract_tb(error.__traceback__)
    ]
