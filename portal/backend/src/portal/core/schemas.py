"""Общие типы тел запросов и ответов API."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, PlainSerializer


def _api_time(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Время в ответах: ISO 8601 в UTC с суффиксом Z (docs/portal-api.md, «Обозначения»).
ApiTime = Annotated[datetime, PlainSerializer(_api_time, return_type=str)]


class RequestModel(BaseModel):
    """Тело запроса: неизвестное поле — ошибка `validation_error` (§1.1)."""

    model_config = ConfigDict(extra="forbid")
