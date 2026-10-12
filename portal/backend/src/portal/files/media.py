"""Хранилище записей (аудио и видео): приём потоком, квота и резервы (docs/portal-api.md §1.6).

Квота считается по диску, а не по таблицам: в неё входят все байты каталога `media/` в
любом состоянии. Резервы идущих загрузок живут в памяти процесса `portal-api` — он один.
"""

import asyncio
import hashlib
import os
import re
import shutil
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from portal.core.settings import MediaSettings
from portal.files.ports import (
    FileTooLargeError,
    MediaQuotaExceededError,
    RecordingType,
    StallReason,
    StoredRecording,
    TooManyUploadsError,
    UnsupportedMediaError,
    UploadStalledError,
)

_AREA = "media"
_SCRATCH = "tmp"
_PART_SUFFIX = ".part"
_UUID = r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}"
_KEY_PATTERN = re.compile(rf"^{_AREA}/{_UUID}/{_UUID}$")
# Столько первых байт нужно, чтобы определить тип (§1.5: тип документа EBML — в первых 64).
_HEAD_BYTES = 64
_EBML = b"\x1a\x45\xdf\xa3"
_FTYP_BRANDS: dict[bytes, RecordingType] = {b"M4A ": "audio/mp4", b"qt  ": "video/quicktime"}
_MPEG_FRAME_SYNC = 0xE0


def detect_recording(head: bytes) -> RecordingType | None:
    """Тип записи по первым байтам файла (сигнатуры §1.5); `None` — не запись."""
    if head.startswith(b"ID3"):
        return "audio/mpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WAVE":
        return "audio/wav"
    if head.startswith(b"fLaC"):
        return "audio/flac"
    if head.startswith(b"OggS"):
        return "audio/ogg"
    if head[4:8] == b"ftyp" and len(head) >= 12:
        return _FTYP_BRANDS.get(head[8:12], "video/mp4")
    if head.startswith(_EBML):
        if b"webm" in head[:_HEAD_BYTES]:
            return "video/webm"
        if b"matroska" in head[:_HEAD_BYTES]:
            return "video/x-matroska"
        return None
    if len(head) >= 2 and head[0] == 0xFF and head[1] & _MPEG_FRAME_SYNC == _MPEG_FRAME_SYNC:
        return "audio/mpeg"
    return None


@dataclass
class _Upload:
    """Идущая загрузка: чей резерв и сколько заявленных байт ещё не записано."""

    owner_id: UUID
    remaining: int


def _tree_size(directory: Path, counted: set[tuple[int, int]] | None = None) -> int:
    """Размер всех файлов каталога с подкаталогами; каталога нет — 0.

    Файл с несколькими именами считается один раз: воркер кладёт в `media/tmp/` жёсткую
    ссылку на исходник записи (§8.7).
    """
    counted = set() if counted is None else counted
    total = 0
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return 0
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                total += _tree_size(Path(entry.path), counted)
            else:
                stat = entry.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) not in counted:
                    counted.add((stat.st_dev, stat.st_ino))
                    total += stat.st_size
        except FileNotFoundError:
            # Файл удалили, пока шёл подсчёт (воркер убрал временный файл) — его байт уже нет.
            continue
    return total


class DiskMediaStorage:
    """Реализация порта `MediaStorage`: `<корень>/media/<owner_id>/<uuid>`."""

    def __init__(self, root: Path, settings: MediaSettings) -> None:
        """Запомнить корень тома `portal-files` и пределы приёма."""
        self._root = root
        self._settings = settings
        self._uploads: dict[UUID, _Upload] = {}
        # Проверка квоты читает диск в пуле потоков; под блокировкой между проверкой и
        # резервом не вклинится другая загрузка (§1.6).
        self._admission = asyncio.Lock()

    async def receive(
        self, owner_id: UUID, chunks: AsyncIterator[bytes], declared_bytes: int
    ) -> StoredRecording:
        """Принять запись потоком; любой отказ и отмена удаляют файл и снимают резерв."""
        upload_id = uuid4()
        async with self._admission:
            self._check_slots(owner_id)
            await self._check_quota(owner_id, declared_bytes)
            self._uploads[upload_id] = _Upload(owner_id, declared_bytes)
        key = self.new_key(owner_id)
        target = self.path(key)
        part = target.with_name(target.name + _PART_SUFFIX)
        try:
            await asyncio.to_thread(part.parent.mkdir, parents=True, exist_ok=True)
            with part.open("wb") as file:
                recording = await self._write(file, chunks, declared_bytes, upload_id, key)
            await asyncio.to_thread(os.replace, part, target)
        except BaseException:
            await asyncio.shield(asyncio.to_thread(part.unlink, missing_ok=True))
            raise
        finally:
            del self._uploads[upload_id]
        return recording

    def _check_slots(self, owner_id: UUID) -> None:
        """Мест загрузки хватает и у сотрудника нет своей идущей загрузки."""
        if len(self._uploads) >= self._settings.max_concurrent_uploads or any(
            upload.owner_id == owner_id for upload in self._uploads.values()
        ):
            raise TooManyUploadsError

    async def _check_quota(self, owner_id: UUID, declared_bytes: int) -> None:
        """Квота портала, квота сотрудника и свободное место — с учётом новой загрузки."""
        area = self._root / _AREA
        used_total, used_owner, free = await asyncio.to_thread(
            lambda: (
                _tree_size(area),
                _tree_size(area / str(owner_id)),
                shutil.disk_usage(self._existing_root()).free,
            )
        )
        reserved_total = sum(upload.remaining for upload in self._uploads.values())
        reserved_owner = sum(
            upload.remaining for upload in self._uploads.values() if upload.owner_id == owner_id
        )
        quota = self._settings.quota_bytes
        if used_total + reserved_total + declared_bytes > quota:
            raise MediaQuotaExceededError("total")
        if used_owner + reserved_owner + declared_bytes > quota * self._settings.user_quota_share:
            raise MediaQuotaExceededError("user")
        if free - reserved_total - declared_bytes < self._settings.min_free_bytes:
            raise MediaQuotaExceededError("free_space")

    def _existing_root(self) -> Path:
        """Каталог тома для запроса свободного места; до первой загрузки корня может не быть."""
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root

    async def _write(
        self,
        file: BinaryIO,
        chunks: AsyncIterator[bytes],
        declared_bytes: int,
        upload_id: UUID,
        key: str,
    ) -> StoredRecording:
        """Записать тело в файл со счётом байт и SHA-256; тип — по первым байтам."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._settings.upload_timeout_seconds
        digest = hashlib.sha256()
        head = b""
        media_type: RecordingType | None = None
        size = 0
        upload = self._uploads[upload_id]
        iterator = aiter(chunks)
        while True:
            chunk = await self._next_chunk(iterator, deadline - loop.time())
            if chunk is None:
                break
            size += len(chunk)
            if size > declared_bytes:
                raise FileTooLargeError
            if media_type is None:
                head += chunk
                if len(head) < _HEAD_BYTES:
                    continue
                media_type = detect_recording(head)
                if media_type is None:
                    raise UnsupportedMediaError
                chunk = head
            digest.update(chunk)
            await asyncio.to_thread(file.write, chunk)
            upload.remaining = declared_bytes - size
        if media_type is None:
            # Тело короче 64 байт: тип определяется по тому, что пришло.
            media_type = detect_recording(head)
            if media_type is None:
                raise UnsupportedMediaError
            digest.update(head)
            await asyncio.to_thread(file.write, head)
        return StoredRecording(
            key=key, size_bytes=size, sha256=digest.digest(), media_type=media_type
        )

    async def _next_chunk(self, iterator: AsyncIterator[bytes], time_left: float) -> bytes | None:
        """Следующая часть тела; `None` — тело кончилось. Сроки — `UploadStalledError`."""
        idle = self._settings.upload_idle_timeout_seconds
        reason: StallReason = "idle" if idle < time_left else "total"
        try:
            async with asyncio.timeout(max(min(idle, time_left), 0)):
                return await anext(iterator, None)
        except TimeoutError:
            raise UploadStalledError(reason) from None

    def new_key(self, owner_id: UUID) -> str:
        """Новый ключ `media/<owner_id>/<uuid>`: для записи и для сжатого звука воркера."""
        return f"{_AREA}/{owner_id}/{uuid4()}"

    def path(self, key: str) -> Path:
        """Путь к файлу; ключ иного вида, чем выдаёт `new_key`, отвергается."""
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("недопустимый ключ записи")
        return self._root / key

    async def delete(self, key: str) -> None:
        """Удалить файл; отсутствие файла — не ошибка."""
        await asyncio.to_thread(self.path(key).unlink, missing_ok=True)

    def _scratch(self, job_id: UUID) -> Path:
        return self._root / _AREA / _SCRATCH / str(job_id)

    def scratch(self, job_id: UUID) -> Path:
        """Пустой временный каталог задания `media/tmp/<job_id>/`; прежнее содержимое удаляется."""
        directory = self._scratch(job_id)
        shutil.rmtree(directory, ignore_errors=True)
        directory.mkdir(parents=True)
        return directory

    async def drop_scratch(self, job_id: UUID) -> None:
        """Удалить временный каталог задания; отсутствие — не ошибка."""
        await asyncio.to_thread(shutil.rmtree, self._scratch(job_id), ignore_errors=True)

    async def clean(self, *, uploads: bool, scratch: bool) -> None:
        """Уборка при старте: недописанные загрузки (`portal serve`), `media/tmp/` (воркер)."""
        area = self._root / _AREA

        def remove() -> None:
            if uploads:
                for part in area.glob(f"*/*{_PART_SUFFIX}"):
                    part.unlink(missing_ok=True)
            if scratch:
                shutil.rmtree(area / _SCRATCH, ignore_errors=True)

        await asyncio.to_thread(remove)
