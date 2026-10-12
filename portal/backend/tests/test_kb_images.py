"""Картинки документов: извлечение, описание, фрагмент-картинка, выдача (§8.6)."""

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from portal.core.settings import Settings
from portal.files.ports import DocumentImage
from portal.kb.ports import VectorIndexUnavailableError
from portal.llm.ports import ModelUnavailableError
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
    assert (fragments[1].start_offset, fragments[1].end_offset) == (0, 0)
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
