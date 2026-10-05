"""Ответ с потоком событий (docs/portal-api.md §6.1)."""

from starlette.responses import StreamingResponse

from portal.core.events import EventChannel


def event_stream_response(channel: EventChannel, keepalive_seconds: float) -> StreamingResponse:
    """Ответ `text/event-stream`; закрытие соединения клиентом закрывает и канал."""
    return StreamingResponse(
        channel.stream(keepalive_seconds), media_type="text/event-stream; charset=utf-8"
    )
