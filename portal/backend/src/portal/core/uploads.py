"""Приём записи (аудио или видео) телом запроса — общее для обоих маршрутов (§1.6).

Маршрут сам проверяет флаг `media.enabled`, право на цель загрузки и параметры строки
запроса, затем зовёт `receive_recording`; после приёма — повторно проверяет сессию и
право перед записью строки.
"""

import asyncio

from starlette.requests import ClientDisconnect, Request

from portal.core import errors
from portal.core.container import Container
from portal.core.ports import SessionInfo
from portal.files.ports import (
    FileTooLargeError,
    MediaQuotaExceededError,
    StoredRecording,
    TooManyUploadsError,
    UnsupportedMediaError,
    UploadStalledError,
)


async def receive_recording(
    request: Request, container: Container, session: SessionInfo
) -> StoredRecording:
    """Принять тело запроса в хранилище записей; отказы приёма — в общем формате ошибок.

    Пока тело принимается, не реже раза в `auth.session.stream_recheck_seconds`
    проверяется, что сессия запроса ещё есть в базе: её удаление обрывает приём — файл
    удаляется, резерв и место загрузки освобождаются, ответ `401 unauthenticated`.
    """
    # Наличие числового `Content-Length` и предел `media.max_bytes` уже проверены
    # до сессии — пределом тела этого маршрута (`PortalMiddleware`).
    declared_bytes = int(request.headers["content-length"])
    recheck_seconds = container.settings.auth.session.stream_recheck_seconds
    reception = asyncio.ensure_future(
        container.media_storage.receive(session.user_id, request.stream(), declared_bytes)
    )
    try:
        while True:
            await asyncio.wait({reception}, timeout=recheck_seconds)
            if reception.done():
                return reception.result()
            if not await container.authenticator.session_exists(session.session_id):
                raise errors.unauthenticated()
    except TooManyUploadsError:
        raise errors.too_many_uploads() from None
    except MediaQuotaExceededError as refusal:
        raise errors.media_quota_exceeded(refusal.reason) from None
    except UnsupportedMediaError:
        raise errors.unsupported_file_type() from None
    except FileTooLargeError:
        raise errors.file_too_large(container.settings.media.max_bytes) from None
    except UploadStalledError as stalled:
        raise errors.upload_timeout(stalled.reason) from None
    except ClientDisconnect:
        # Отвечать некому; обычный отказ вместо записи о сбое в журнале.
        raise errors.bad_request() from None
    finally:
        if not reception.done():
            reception.cancel()
            await asyncio.gather(reception, return_exceptions=True)
