"""Приём записей: тип по содержимому, квота, резервы, сроки (docs/portal-api.md §1.5, §1.6)."""

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from portal.core.settings import Settings
from portal.files.media import DiskMediaStorage, detect_recording
from portal.files.ports import (
    FileTooLargeError,
    MediaQuotaExceededError,
    TooManyUploadsError,
    UnsupportedMediaError,
    UploadStalledError,
)

pytestmark = pytest.mark.anyio

WAV = b"RIFF\x00\x00\x00\x00WAVEfmt " + b"\x00" * 100
OWNER = UUID("11111111-1111-4111-8111-111111111111")
OTHER = UUID("22222222-2222-4222-8222-222222222222")


@pytest.fixture
def make_storage(settings: Settings, tmp_path: Path) -> Callable[..., DiskMediaStorage]:
    """Хранилище во временном каталоге с маленькой квотой; пределы тест задаёт сам."""

    def make(**overrides: float) -> DiskMediaStorage:
        values: dict[str, float] = {
            "quota_bytes": 1000,
            "user_quota_share": 0.5,
            "min_free_bytes": 0,
            "max_concurrent_uploads": 2,
            "upload_idle_timeout_seconds": 5,
            "upload_timeout_seconds": 5,
        }
        return DiskMediaStorage(tmp_path, settings.media.model_copy(update={**values, **overrides}))

    return make


async def _chunks(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


async def _held(first: bytes, release: asyncio.Event, rest: bytes = b"") -> AsyncIterator[bytes]:
    """Тело, которое после первой части ждёт разрешения теста."""
    yield first
    await release.wait()
    if rest:
        yield rest


def _files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"ID3\x04\x00", "audio/mpeg"),
        (b"\xff\xfb\x90\x64", "audio/mpeg"),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"fLaC\x00\x00\x00\x22", "audio/flac"),
        (b"OggS\x00\x02", "audio/ogg"),
        (b"\x00\x00\x00\x20ftypM4A \x00\x00\x00\x00", "audio/mp4"),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00", "video/quicktime"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00", "video/mp4"),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x86\x81\x01\x42\x82\x84webm", "video/webm"),
        (b"\x1a\x45\xdf\xa3\xa3\x42\x86\x81\x01\x42\x82\x88matroska", "video/x-matroska"),
        # Не записи: изображение, PDF, AVI, плейлист HLS, EBML иного типа, пустое тело.
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", None),
        (b"%PDF-1.7", None),
        (b"RIFF\x24\x00\x00\x00AVI LIST", None),
        (b"#EXTM3U\n#EXT-X-VERSION:3", None),
        (b"\x1a\x45\xdf\xa3\x9f\x42\x82\x84other", None),
        (b"", None),
    ],
)
def test_recording_type_is_detected_by_signature(head: bytes, expected: str | None) -> None:
    assert detect_recording(head) == expected


async def test_recording_is_received_by_parts_into_owner_directory(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    storage = make_storage()
    stored = await storage.receive(OWNER, _chunks(WAV[:10], WAV[10:70], WAV[70:]), len(WAV))
    area, owner, name = stored.key.split("/")
    assert (area, owner, len(name)) == ("media", str(OWNER), 36)
    assert stored.media_type == "audio/wav" and stored.size_bytes == len(WAV)
    assert stored.sha256 == hashlib.sha256(WAV).digest()
    assert storage.path(stored.key).read_bytes() == WAV
    assert _files(tmp_path) == [storage.path(stored.key)]  # файла .part не осталось
    await storage.delete(stored.key)
    await storage.delete(stored.key)  # отсутствие файла — не ошибка
    assert _files(tmp_path) == []


@pytest.mark.parametrize(
    "key",
    [
        "media/../../etc/passwd",
        f"media/{OWNER}/../x",
        f"media/{OWNER}",
        f"kb/{OWNER}",
        f"media/tmp/{OWNER}",
        "",
    ],
)
def test_storage_rejects_keys_it_did_not_issue(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path, key: str
) -> None:
    with pytest.raises(ValueError, match="ключ"):
        make_storage().path(key)


async def test_refusals_during_receiving_leave_no_file_and_no_reserve(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    storage = make_storage(quota_bytes=400, user_quota_share=1)
    with pytest.raises(UnsupportedMediaError):
        await storage.receive(OWNER, _chunks(b"%PDF-1.7" + b"x" * 100), 108)
    with pytest.raises(UnsupportedMediaError):
        await storage.receive(OWNER, _chunks(b"short"), 5)
    # Тело длиннее заявленного: приём обрывается на первой лишней части.
    consumed: list[int] = []

    async def longer() -> AsyncIterator[bytes]:
        for number in range(10):
            consumed.append(number)
            yield WAV

    with pytest.raises(FileTooLargeError):
        await storage.receive(OWNER, longer(), len(WAV) + 1)
    assert consumed == [0, 1]

    async def broken() -> AsyncIterator[bytes]:
        yield WAV
        raise ConnectionError

    with pytest.raises(ConnectionError):
        await storage.receive(OWNER, broken(), 300)
    assert _files(tmp_path) == []
    # Резервы сняты: квота 400 снова свободна целиком.
    stored = await storage.receive(OWNER, _chunks(WAV), 400)
    assert stored.size_bytes == len(WAV)


async def test_quota_counts_disk_and_reserves(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    storage = make_storage(quota_bytes=1000, user_quota_share=0.5)
    # Сотруднику — половина квоты: 500 байт.
    with pytest.raises(MediaQuotaExceededError) as refusal:
        await storage.receive(OWNER, _chunks(WAV), 501)
    assert refusal.value.reason == "user"
    await storage.receive(OWNER, _chunks(WAV), len(WAV))
    with pytest.raises(MediaQuotaExceededError) as refusal:
        await storage.receive(OWNER, _chunks(WAV), 501 - len(WAV))
    assert refusal.value.reason == "user"
    # В квоту входит всё, что лежит в каталоге media/: сжатый звук и временные файлы воркера.
    scratch = storage.scratch(uuid4())
    (scratch / "part.wav").write_bytes(b"x" * 600)
    with pytest.raises(MediaQuotaExceededError) as refusal:
        await storage.receive(OTHER, _chunks(WAV), 400)
    assert refusal.value.reason == "total"
    assert [path.name for path in _files(tmp_path / "media" / str(OTHER))] == []


async def test_second_upload_that_fits_only_alone_is_refused(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    """Две загрузки, превышающие квоту только вместе: вторая получает отказ (критерий 24)."""
    storage = make_storage(quota_bytes=1000, user_quota_share=1)
    release = asyncio.Event()
    first = asyncio.create_task(storage.receive(OWNER, _held(WAV, release), 600))
    await asyncio.sleep(0.05)
    with pytest.raises(MediaQuotaExceededError) as refusal:
        await storage.receive(OTHER, _chunks(WAV), 600)
    assert refusal.value.reason == "total"
    release.set()
    assert (await first).size_bytes == len(WAV)
    # Резерв снят вместе с окончанием приёма: на диске только принятые байты.
    assert (await storage.receive(OTHER, _chunks(WAV), 600)).size_bytes == len(WAV)


async def test_low_free_space_refuses_whatever_the_quota(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    storage = make_storage(min_free_bytes=2**62)
    with pytest.raises(MediaQuotaExceededError) as refusal:
        await storage.receive(OWNER, _chunks(WAV), len(WAV))
    assert refusal.value.reason == "free_space"
    assert _files(tmp_path) == []


async def test_upload_slots_one_per_employee_and_limited_in_total(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    storage = make_storage(quota_bytes=10**6, max_concurrent_uploads=2)
    release = asyncio.Event()
    first = asyncio.create_task(storage.receive(OWNER, _held(WAV, release), 200))
    await asyncio.sleep(0.05)
    with pytest.raises(TooManyUploadsError):
        await storage.receive(OWNER, _chunks(WAV), 200)
    second = asyncio.create_task(storage.receive(OTHER, _held(WAV, release), 200))
    await asyncio.sleep(0.05)
    with pytest.raises(TooManyUploadsError):
        await storage.receive(uuid4(), _chunks(WAV), 200)
    release.set()
    await asyncio.gather(first, second)
    # Места освободились.
    await storage.receive(OWNER, _chunks(WAV), 200)


async def test_stalled_and_endless_uploads_are_cut(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    never = asyncio.Event()
    idle = make_storage(upload_idle_timeout_seconds=0.05, upload_timeout_seconds=5)
    with pytest.raises(UploadStalledError) as refusal:
        await idle.receive(OWNER, _held(WAV, never), 300)
    assert refusal.value.reason == "idle"

    async def drip() -> AsyncIterator[bytes]:
        yield WAV
        while True:
            await asyncio.sleep(0.02)
            yield b"x"

    total = make_storage(
        quota_bytes=10**6, upload_idle_timeout_seconds=1, upload_timeout_seconds=0.1
    )
    with pytest.raises(UploadStalledError) as refusal:
        await total.receive(OWNER, drip(), 5000)
    assert refusal.value.reason == "total"
    assert _files(tmp_path) == []
    # Место загрузки и резерв освобождены.
    await idle.receive(OWNER, _chunks(WAV), len(WAV))


async def test_cancelled_upload_is_cleaned_up(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    """Отмена вызова (сессия пропала во время приёма) — то же, что обрыв соединения."""
    storage = make_storage(quota_bytes=300, user_quota_share=1)
    task = asyncio.create_task(storage.receive(OWNER, _held(WAV, asyncio.Event()), 300))
    await asyncio.sleep(0.05)
    assert [path.suffix for path in _files(tmp_path)] == [".part"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _files(tmp_path) == []
    await storage.receive(OWNER, _chunks(WAV), 300)


async def test_startup_cleaning(
    make_storage: Callable[..., DiskMediaStorage], tmp_path: Path
) -> None:
    storage = make_storage()
    stored = await storage.receive(OWNER, _chunks(WAV), len(WAV))
    part = storage.path(stored.key).with_name(f"{uuid4()}.part")
    part.write_bytes(b"half")
    job_id = uuid4()
    scratch = storage.scratch(job_id)
    (scratch / "audio.wav").write_bytes(b"x")
    assert list(storage.scratch(job_id).iterdir()) == []  # каталог задания выдаётся пустым
    (scratch / "audio.wav").write_bytes(b"x")

    await storage.clean(uploads=True, scratch=False)
    assert not part.exists() and scratch.exists()
    await storage.clean(uploads=False, scratch=True)
    assert _files(tmp_path) == [storage.path(stored.key)]
    await storage.drop_scratch(job_id)  # отсутствие каталога — не ошибка


async def test_hard_link_of_a_source_in_scratch_is_counted_once(
    make_storage: Callable[..., DiskMediaStorage],
) -> None:
    """Воркер кладёт в `media/tmp/` жёсткую ссылку на исходник: в квоте он один (§8.7)."""
    storage = make_storage(quota_bytes=300, user_quota_share=1.0)
    stored = await storage.receive(OWNER, _chunks(WAV), len(WAV))
    os.link(storage.path(stored.key), storage.scratch(uuid4()) / "source")
    # Занято len(WAV), а не вдвое больше: вторая запись того же размера помещается.
    assert 2 * len(WAV) <= 300 < 3 * len(WAV)
    await storage.receive(OTHER, _chunks(WAV), len(WAV))
    with pytest.raises(MediaQuotaExceededError) as refusal:
        await storage.receive(OTHER, _chunks(WAV), len(WAV))
    assert refusal.value.reason == "total"
