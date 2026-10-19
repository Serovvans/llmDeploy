"""Картинки документов: извлечение, описание, фрагмент-картинка, выдача (§8.6)."""

import hashlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from portal.core.settings import Settings
from portal.files.ports import DocumentImage
from portal.kb.ports import VectorIndexUnavailableError
from portal.llm.ports import Completion, ModelUnavailableError
from tests import samples
from tests.conftest import close_portal, make_kb_bench
from tests.kb_support import SHARED, KbBench, added, kb_settings
from tests.support import Portal

pytestmark = pytest.mark.anyio

IMAGE_PROMPT = "Опиши изображение из документа."
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

type MakeBench = Callable[..., Awaitable[KbBench]]


@pytest.fixture
async def image_bench(settings: Settings, tmp_path: Path) -> AsyncIterator[MakeBench]:
    """Стенд с параметрами картинок и воркера, которые задаёт тест."""
    built: list[KbBench] = []

    async def make(worker: dict[str, Any] | None = None, **images: Any) -> KbBench:
        tuned = settings.kb.images.model_copy(update=images)
        built.append(
            await make_kb_bench(kb_settings(settings, tmp_path, images=tuned, worker=worker or {}))
        )
        return built[-1]

    yield make
    for bench in built:
        await close_portal(bench.portal)


async def _images(portal: Portal) -> list[Any]:
    return await portal.rows(
        "SELECT id, page_number, storage_key, media_type, description FROM kb_images"
        " ORDER BY ordinal"
    )


def _image_files(portal: Portal) -> list[Path]:
    return sorted((portal.container.settings.files.root / "kb-images").glob("*"))


def _descriptions_asked(bench: KbBench) -> int:
    return bench.model.prompts.count(IMAGE_PROMPT)


async def test_pictures_are_described_once_and_found_as_sources(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    red, blue = samples.png(300, 200, "red"), samples.png(320, 240, "blue")
    # Маленькая картинка ниже порогов, вторая красная — повтор первой.
    content = samples.docx_with_pictures(red, samples.png(40, 30), blue, red)
    document_id = await added(client, "схема [1].docx", content)
    await bench.drain()

    first, second = await _images(portal)
    assert (first.page_number, first.media_type) == (None, "image/png")
    assert first.description.startswith("Описание изображения-заглушки")
    assert first.description != second.description
    assert _descriptions_asked(bench) == 2 and len(_image_files(portal)) == 2
    document = (await client.get(f"/api/kb/documents/{document_id}")).json()
    assert (document["status"], document["images_truncated"]) == ("ready", False)

    found = await bench.knowledge.retrieve(
        second.description, await portal.user_id("ivanov"), SHARED
    )
    source = found.sources[0]
    assert (source.image_id, source.quote, source.page) == (second.id, second.description, None)
    assert source.timecode_seconds is None
    # Модель получает описание, а не картинку; строка-заголовок помечает источник.
    assert f"[1] схема (1).docx, изображение\n<фрагмент>\n{second.description}" in found.context
    # Фрагмент-картинка соседей не получает: текста документа рядом с описанием нет.
    assert "Текст перед картинками" not in found.context.split("[2]")[0]

    text = (
        await client.get(
            f"/api/kb/documents/{document_id}/text", params={"fragment_id": str(source.fragment_id)}
        )
    ).json()
    assert text["image"] == {"id": str(second.id), "description": second.description}
    assert [segment["highlight"] for segment in text["segments"]] == [False]


async def test_picture_is_served_only_to_those_who_see_its_document(bench: KbBench) -> None:
    portal = bench.portal
    ivanov = await portal.employee("ivanov")
    petrov = await portal.employee("petrov")
    admin = portal.client()
    await portal.onboard(admin, "boss", "admin")
    personal = await added(
        ivanov, "личное.docx", samples.docx_with_pictures(samples.png(300, 200)), "personal"
    )
    shared = await added(petrov, "общий.txt", "Общий документ".encode())
    await bench.drain()
    (image,) = await _images(portal)
    path = f"/api/kb/documents/{personal}/images/{image.id}"

    served = await ivanov.get(path)
    assert served.status_code == 200 and served.content.startswith(PNG_SIGNATURE)
    assert served.headers["content-type"] == "image/png"
    assert served.headers["x-content-type-options"] == "nosniff"
    assert served.headers["content-disposition"] == "inline"

    # Чужая личная картинка не существует — и для администратора тоже.
    for stranger in (petrov, admin):
        assert (await stranger.get(path)).status_code == 404
    # Картинка обязана принадлежать документу из адреса: через свой документ чужую не взять.
    assert (await petrov.get(f"/api/kb/documents/{shared}/images/{image.id}")).status_code == 404
    assert (await ivanov.get(f"/api/kb/documents/{shared}/images/{image.id}")).status_code == 404
    assert (await ivanov.get(f"/api/kb/documents/{personal}/images/x")).status_code == 404
    assert (await portal.client().get(path)).status_code == 401

    # Удалённый документ картинку не отдаёт сразу; файл стирает воркер.
    assert (await ivanov.delete(f"/api/kb/documents/{personal}")).status_code == 204
    assert (await ivanov.get(path)).status_code == 404
    assert len(_image_files(portal)) == 1
    await bench.drain()
    assert _image_files(portal) == [] and await _images(portal) == []


async def test_limit_marks_the_document_and_reprocessing_keeps_described_pictures(
    image_bench: MakeBench,
) -> None:
    bench = await image_bench(max_per_document=1)
    portal = bench.portal
    client = await portal.employee()
    content = samples.docx_with_pictures(samples.png(300, 200, "red"), samples.png(300, 200))
    document_id = await added(client, "много картинок.docx", content)
    await bench.drain()

    (kept,) = await _images(portal)
    assert (await client.get(f"/api/kb/documents/{document_id}")).json()["images_truncated"]
    assert _descriptions_asked(bench) == 1

    # Повторная обработка: картинка с тем же содержимым сохраняет строку и описание.
    assert await bench.reindexer.run(only_errors=False, recreate_collection=False) == 1
    await bench.drain()
    (again,) = await _images(portal)
    assert (again.id, again.description, again.storage_key) == (
        kept.id,
        kept.description,
        kept.storage_key,
    )
    assert _descriptions_asked(bench) == 1 and len(_image_files(portal)) == 1
    fragments = await portal.rows("SELECT image_id FROM kb_fragments WHERE image_id IS NOT NULL")
    assert [fragment.image_id for fragment in fragments] == [kept.id]


async def test_failed_reprocessing_leaves_pictures_and_their_limit_mark_as_they_were(
    image_bench: MakeBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой переобработки: документ остаётся в прежнем виде — и по картинкам тоже (§11.6)."""
    bench = await image_bench(max_per_document=2)
    portal = bench.portal
    client = await portal.employee()
    content = samples.docx_with_pictures(samples.png(300, 200, "red"))
    document_id = await added(client, "схема.docx", content)
    await bench.drain()
    before = await _images(portal)
    assert len(before) == 1 and _descriptions_asked(bench) == 1

    # Переобработка находит в файле больше картинок, чем лимит, и падает на записи запаса.
    found_now = [
        DocumentImage(samples.png(310, 210, color), "image/png", 310, 210, digest)
        for color, digest in (
            ("blue", hashlib.sha256(b"blue").digest()),
            ("green", hashlib.sha256(b"green").digest()),
        )
    ]
    original = bench.reader.embedded_images

    def more(*args: Any, **kwargs: Any) -> list[DocumentImage]:
        return [*original(*args, **kwargs), *found_now]

    async def broken(*_: Any) -> None:
        raise VectorIndexUnavailableError

    monkeypatch.setattr(bench.reader, "embedded_images", more)
    monkeypatch.setattr(bench.index, "stage_document", broken)
    assert await bench.reprocessor.start(all_ready=True) == 1
    for _ in range(bench.settings.worker.max_attempts):
        assert await bench.drain() == 1
        await portal.execute("UPDATE kb_jobs SET run_after = :now", now=portal.clock.now())
        shown = (await client.get(f"/api/kb/documents/{document_id}")).json()
        assert shown["images_truncated"] is False  # признак меняется только с индексом

    shown = (await client.get(f"/api/kb/documents/{document_id}")).json()
    assert (shown["status"], shown["reprocess_error"]) == ("ready", "internal_error")
    assert shown["images_truncated"] is False
    assert await _images(portal) == before and len(_image_files(portal)) == 1
    # Пока шли повторы, новая картинка описывалась один раз, а не на каждой попытке.
    assert _descriptions_asked(bench) == 2


async def test_picture_file_is_both_a_scan_and_a_picture(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    await added(client, "фото.png", samples.png(300, 200, "green"))
    await bench.drain()

    (image,) = await _images(portal)
    fragments = await portal.rows(
        "SELECT page_number, image_id, start_offset, end_offset FROM kb_fragments ORDER BY ordinal"
    )
    # Текст распознан как страница-скан, сам файл — одна картинка на той же странице.
    assert [(f.page_number, f.image_id) for f in fragments] == [(1, None), (1, image.id)]
    # Границы фрагмента-картинки — его часть описания; короткое описание — целиком.
    assert (fragments[1].start_offset, fragments[1].end_offset) == (0, len(image.description))
    assert _image_files(portal)[0].read_bytes().startswith(PNG_SIGNATURE)


async def test_picture_without_description_is_not_kept(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    bench.model.answer = "  "
    document_id = await added(
        client, "пусто.docx", samples.docx_with_pictures(samples.png(300, 200))
    )
    await bench.drain()

    assert await _images(portal) == [] and _image_files(portal) == []
    assert (await client.get(f"/api/kb/documents/{document_id}")).json()["status"] == "ready"
    assert await portal.rows("SELECT 1 FROM kb_fragments WHERE image_id IS NOT NULL") == []


async def test_disabled_flag_extracts_nothing(image_bench: MakeBench) -> None:
    bench = await image_bench(enabled=False)
    client = await bench.portal.employee()
    await added(client, "схема.docx", samples.docx_with_pictures(samples.png(300, 200)))
    await bench.drain()

    assert await _images(bench.portal) == [] and _descriptions_asked(bench) == 0


async def test_model_failure_on_description_ends_as_recognition_failed(
    image_bench: MakeBench,
) -> None:
    bench = await image_bench(worker={"max_attempts": 1})
    portal = bench.portal
    client = await portal.employee()
    bench.model.fail_when = IMAGE_PROMPT
    bench.model.error = ModelUnavailableError()
    document_id = await added(
        client, "схема.docx", samples.docx_with_pictures(samples.png(300, 200))
    )
    await bench.drain()

    document = (await client.get(f"/api/kb/documents/{document_id}")).json()
    assert (document["status"], document["error_code"]) == ("error", "recognition_failed")
    # Извлечённая картинка сохранена: повторная обработка продолжит с описания.
    (image,) = await _images(portal)
    assert image.description is None

    bench.model.fail_when = None
    assert (await client.post(f"/api/kb/documents/{document_id}/retry")).status_code == 200
    await bench.drain()
    (described,) = await _images(portal)
    assert described.id == image.id and described.description


def _image_log(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """Записи журнала об описаниях: сообщение и причина; текста описаний в них нет."""
    records = [record for record in caplog.records if record.name == "portal.kb.images"]
    for record in records:
        assert {"document_id", "image_id", "reason"} <= set(vars(record))
        assert "Описание" not in record.getMessage() and "Ж" not in str(vars(record))
    return [(record.getMessage(), record.__dict__["reason"]) for record in records]


async def test_stub_widths_drop_split_and_retry_descriptions(
    bench: KbBench, caplog: pytest.LogCaptureFixture
) -> None:
    """Ширины заглушки 701–703 (§12.2): пустое описание, длинное, упёршееся в предел."""
    portal = bench.portal
    client = await portal.employee()
    pictures = samples.png(701, 200), samples.png(702, 200), samples.png(703, 200)
    await added(client, "скриншоты.docx", samples.docx_with_pictures(*pictures))
    with caplog.at_level(logging.WARNING, logger="portal.kb.images"):
        await bench.drain()

    # Пустое описание: картинка отброшена при замене индекса, её файла нет.
    long, full = await portal.rows(
        "SELECT id, ordinal, description, description_version, pending_description"
        " FROM kb_images ORDER BY ordinal"
    )
    assert [(row.ordinal, row.description_version) for row in (long, full)] == [(0, 2), (1, 2)]
    assert len(_image_files(portal)) == 2 and long.pending_description is None
    assert _image_log(caplog) == [("image dropped", "empty")]
    # Упёршееся в предел описание запрошено ещё раз с бо́льшим пределом и пришло целиком.
    images = bench.settings.images
    assert full.description.startswith("Описание изображения-заглушки")
    assert sorted(tokens or 0 for tokens in bench.model.max_tokens) == [
        images.description_max_tokens
    ] * 3 + [images.description_retry_max_tokens]

    # Длинное описание — несколько фрагментов одной картинки по правилам длины фрагмента.
    parts = await portal.rows(
        "SELECT ordinal, start_offset, end_offset FROM kb_fragments WHERE image_id = :id"
        " ORDER BY ordinal",
        id=long.id,
    )
    max_chars = bench.settings.chunking.max_chars
    assert len(long.description) > 3000 and len(parts) >= 3
    assert parts[0].start_offset == 0 and parts[-1].end_offset == len(long.description)
    assert all(0 < part.end_offset - part.start_offset <= max_chars for part in parts)
    texts = await portal.rows("SELECT ordinal FROM kb_fragments WHERE image_id IS NULL")
    assert min(part.ordinal for part in parts) > max(row.ordinal for row in texts)

    # Источник — часть описания: она и цитата, и текст для модели.
    found = await bench.knowledge.retrieve("строка 40", await portal.user_id("ivanov"), SHARED)
    source = next(source for source in found.sources if source.image_id == long.id)
    assert source.quote in long.description and len(source.quote) <= max_chars
    assert "строка 40." in source.quote and source.quote != long.description


async def test_descriptions_cut_by_limits_are_kept_or_dropped_and_logged(
    image_bench: MakeBench, caplog: pytest.LogCaptureFixture
) -> None:
    """Упор в предел и после повтора, обрезка по длине (§8.6); текст в журнал не попадает."""
    bench = await image_bench(worker={"background_llm_parallel": 1})
    portal = bench.portal
    client = await portal.employee()
    bench.model.completions = [
        Completion("", "length"),
        Completion("", "length"),
        Completion("Нача", "length"),
        Completion("  Начало описания схемы\x00 ", "length"),
        Completion("Ж" * 5000, "stop"),
    ]
    pictures = (samples.png(300, 200, color) for color in ("red", "green", "blue"))
    await added(client, "схемы.docx", samples.docx_with_pictures(*pictures))
    with caplog.at_level(logging.WARNING, logger="portal.kb.images"):
        await bench.drain()

    rows = await _images(portal)
    max_chars = bench.settings.images.description_max_chars
    assert [row.description for row in rows] == ["Начало описания схемы", "Ж" * max_chars]
    images = bench.settings.images
    first, retry = images.description_max_tokens, images.description_retry_max_tokens
    assert bench.model.max_tokens == [first, retry, first, retry, first]
    assert _image_log(caplog) == [
        ("image dropped", "length"),
        ("image description truncated", "length"),
        ("image description truncated", "max_chars"),
    ]


async def test_old_descriptions_are_replaced_only_together_with_the_index(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Переобработка 0.16 (§8.6, §11.6): пока индекс не заменён, ищется прежнее описание."""
    portal = bench.portal
    client = await portal.employee()
    user = await portal.user_id("ivanov")
    await added(client, "схема.docx", samples.docx_with_pictures(samples.png(300, 200)))
    await added(client, "заметка.txt", "Текстовая заметка без картинок.".encode())
    await bench.drain()
    (image,) = await _images(portal)
    # Как до 0.16: описание прежних правил, фрагмент-картинка — описание целиком.
    await portal.execute(
        "UPDATE kb_images SET description = 'Старое описание схемы', description_version = 1"
    )
    await portal.execute(
        "UPDATE kb_fragments SET start_offset = 0, end_offset = 0 WHERE image_id IS NOT NULL"
    )
    asked = _descriptions_asked(bench)
    assert (await bench.reprocessor.status()).old_descriptions == 1
    assert await bench.reprocessor.start(all_ready=False, images=True) == 1  # TXT не берётся

    async def broken(*_: Any) -> None:
        raise VectorIndexUnavailableError

    original = bench.index.stage_document
    monkeypatch.setattr(bench.index, "stage_document", broken)
    assert await bench.drain() == 1
    (pending,) = await portal.rows(
        "SELECT description, description_version, pending_description FROM kb_images"
    )
    assert (pending.description, pending.description_version) == ("Старое описание схемы", 1)
    assert pending.pending_description == image.description
    found = await bench.knowledge.retrieve(image.description, user, SHARED)
    assert [source.quote for source in found.sources if source.image_id] == [
        "Старое описание схемы"
    ]

    monkeypatch.setattr(bench.index, "stage_document", original)
    await portal.execute("UPDATE kb_jobs SET run_after = :now", now=portal.clock.now())
    assert await bench.drain() == 1
    (replaced,) = await portal.rows(
        "SELECT id, description, description_version, pending_description FROM kb_images"
    )
    assert tuple(replaced) == (image.id, image.description, 2, None)
    assert _descriptions_asked(bench) == asked + 1  # новое описание из попытки не потеряно
    assert (await bench.reprocessor.status()).old_descriptions == 0
    found = await bench.knowledge.retrieve(image.description, user, SHARED)
    assert [source.quote for source in found.sources if source.image_id] == [image.description]


async def test_reprocessing_orders_pictures_by_new_index_and_passes_saved_ones_as_known(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    await added(client, "схема.docx", samples.docx_with_pictures(samples.png(300, 200, "red")))
    await bench.drain()
    (red,) = await portal.rows("SELECT id, sha256 FROM kb_images")
    blue = DocumentImage(
        samples.png(310, 210, "blue"), "image/png", 310, 210, hashlib.sha256(b"blue").digest()
    )
    original = bench.reader.embedded_images
    known: list[frozenset[bytes]] = []

    def blue_first(*args: Any, **kwargs: Any) -> list[DocumentImage]:
        known.append(kwargs["known"])
        return [blue, *original(*args, **kwargs)]

    monkeypatch.setattr(bench.reader, "embedded_images", blue_first)
    assert await bench.reprocessor.start(all_ready=True) == 1
    await bench.drain()

    # Сохранённая картинка не декодируется заново; порядок — по новому индексу.
    assert known == [frozenset({bytes(red.sha256)})]
    rows = await portal.rows("SELECT id, ordinal, sha256 FROM kb_images ORDER BY ordinal")
    assert [(bytes(row.sha256), row.ordinal) for row in rows] == [
        (blue.sha256, 0),
        (bytes(red.sha256), 1),
    ]
    assert rows[1].id == red.id
    fragments = await portal.rows(
        "SELECT image_id FROM kb_fragments WHERE image_id IS NOT NULL ORDER BY ordinal"
    )
    assert [fragment.image_id for fragment in fragments] == [rows[0].id, red.id]
