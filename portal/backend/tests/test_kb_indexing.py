"""Конвейер индексации и очередь: страницы, идемпотентность, повторы, удаление (§11)."""

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from portal.core.settings import Settings
from portal.files import text as text_module
from portal.files.ports import UnreadableDocumentError
from portal.kb.ports import CollectionMismatchError, VectorIndexUnavailableError
from portal.kb.qdrant import QdrantVectorIndex
from portal.kb.reindex import WorkerRunningError
from portal.worker.health import heartbeat_is_fresh
from portal.worker.loop import Worker
from tests import samples
from tests.conftest import close_portal, make_portal
from tests.kb_support import KbBench, added, kb_settings, make_bench

pytestmark = pytest.mark.anyio

LONG_PAGE = " ".join(f"Sentence number {n} of the long lease agreement page." for n in range(90))


async def _status(bench: KbBench, document_id: str) -> tuple[str, str | None]:
    row = (
        await bench.portal.rows(
            "SELECT status, error_code FROM kb_documents WHERE id = :id", id=document_id
        )
    )[0]
    return row.status, row.error_code


async def _jobs(bench: KbBench) -> list[Any]:
    return await bench.portal.rows(
        "SELECT kind, attempts, run_after, locked_until FROM kb_jobs ORDER BY created_at"
    )


async def _exhaust_attempts(bench: KbBench) -> None:
    """Довести задание до последней попытки, не двигая часы: сессия теста не истекает."""
    for _ in range(bench.settings.worker.max_attempts):
        await bench.drain()
        await bench.portal.execute(
            "UPDATE kb_jobs SET run_after = :now", now=bench.portal.clock.now()
        )


# --- разбор каждого формата ---


async def test_text_pdf_is_split_into_fragments_that_keep_page_numbers(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    pages = [samples.TEXT_LAYER, LONG_PAGE, "Third page mentions parcel 77:01:0004012:345"]
    document_id = await added(client, "договор.pdf", samples.text_pdf(pages))
    assert await bench.drain() == 1

    assert await _status(bench, document_id) == ("ready", None)
    stored = await portal.rows("SELECT number, text, recognized FROM kb_pages ORDER BY number")
    assert [(page.number, page.text, page.recognized) for page in stored] == [
        (1, pages[0], False),
        (2, LONG_PAGE, False),
        (3, pages[2], False),
    ]
    fragments = await portal.rows("SELECT * FROM kb_fragments ORDER BY ordinal")
    assert [fragment.ordinal for fragment in fragments] == list(range(len(fragments)))
    assert [fragment.page_number for fragment in fragments] == sorted(
        fragment.page_number for fragment in fragments
    )
    assert {fragment.page_number for fragment in fragments} == {1, 2, 3}
    assert sum(1 for fragment in fragments if fragment.page_number == 2) > 2
    for fragment in fragments:
        text = pages[fragment.page_number - 1][fragment.start_offset : fragment.end_offset]
        assert 0 < len(text) <= bench.settings.chunking.max_chars  # в границах своей страницы
    assert await bench.points() == len(fragments)
    row = (await portal.rows("SELECT pages_done, recognizing FROM kb_documents"))[0]
    assert (row.pages_done, row.recognizing) == (3, False)
    assert await _jobs(bench) == []
    assert bench.recognizer.calls == []


async def test_scan_pages_are_recognized_and_mixed_pdf_keeps_text_layer(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    mixed = samples.text_pdf([samples.TEXT_LAYER, "", "", ""])
    document_id = await added(client, "смешанный.pdf", mixed)
    await bench.drain()

    assert await _status(bench, document_id) == ("ready", None)
    stored = await portal.rows("SELECT number, text, recognized FROM kb_pages ORDER BY number")
    assert [(page.number, page.recognized) for page in stored] == [
        (1, False),
        (2, True),
        (3, True),
        (4, True),
    ]
    assert all(page.text.startswith("Распознанный текст скана") for page in stored[1:])
    # Сканы распознаются пачками: одновременно в памяти не больше двух растров.
    assert bench.recognizer.calls == [2, 1]
    assert (await portal.rows("SELECT recognizing FROM kb_documents"))[0].recognizing is True
    body = (await client.get(f"/api/kb/documents/{document_id}/text", params={"page": 2})).json()
    assert body["recognized"] is True


@pytest.mark.parametrize(
    ("name", "content", "expected", "recognized"),
    [
        ("регламент.docx", samples.docx_file("Регламент копирования", "Пункт 1"), "Пункт 1", False),
        ("заметка.txt", "Заметка о сервитуте № 7-С".encode(), "сервитуте № 7-С", False),
        ("старый.md", "# Памятка\n\nтекст в cp1251".encode("cp1251"), "текст в cp1251", False),
        ("фото.png", samples.png(), "Распознанный текст скана", True),
        ("фото.jpg", samples.jpeg(), "Распознанный текст скана", True),
    ],
)
async def test_other_formats_become_single_page(
    bench: KbBench, name: str, content: bytes, expected: str, recognized: bool
) -> None:
    client = await bench.portal.employee()
    document_id = await added(client, name, content)
    await bench.drain()
    assert await _status(bench, document_id) == ("ready", None)
    pages = await bench.portal.rows("SELECT number, text, recognized FROM kb_pages")
    assert [(page.number, page.recognized) for page in pages] == [(1, recognized)]
    assert expected in pages[0].text
    assert await bench.points() >= 1


# --- постоянные сбои ---


async def test_document_without_text_gets_no_text_error(bench: KbBench) -> None:
    client = await bench.portal.employee()
    bench.recognizer.text = ""
    scan = await added(client, "пустой скан.png", samples.png())
    blank = await added(client, "пустой.txt", b" \n\t ")
    await bench.drain()
    assert await _status(bench, scan) == ("error", "no_text")
    assert await _status(bench, blank) == ("error", "no_text")
    assert await _jobs(bench) == []
    assert await bench.points() == 0
    # Повторная обработка разрешена, но причина в файле — исход тот же.
    assert (await client.post(f"/api/kb/documents/{blank}/retry")).status_code == 200
    await bench.drain()
    assert await _status(bench, blank) == ("error", "no_text")


async def test_file_that_stops_opening_gets_file_unreadable(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "договор.pdf", samples.text_pdf([samples.TEXT_LAYER]))
    key = (await portal.rows("SELECT storage_key FROM kb_documents"))[0].storage_key
    (portal.container.settings.files.root / key).write_bytes(b"%PDF-1.4 broken")
    await bench.drain()
    assert await _status(bench, document_id) == ("error", "file_unreadable")
    assert await _jobs(bench) == []


@pytest.mark.parametrize("method", ["page_text", "page_image"])
async def test_unreadable_page_is_a_permanent_error_without_retries(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Не читается отдельная страница: `file_unreadable` сразу, первой попыткой (§11.2)."""
    client = await bench.portal.employee()
    pages = [samples.TEXT_LAYER, ""] if method == "page_image" else [samples.TEXT_LAYER] * 2
    document_id = await added(client, "договор.pdf", samples.text_pdf(pages))
    reader = bench.reader
    original = getattr(reader, method)

    def broken_second_page(path: Path, media_type: Any, page: int) -> Any:
        if page == 2:
            raise UnreadableDocumentError
        return original(path, media_type, page)

    monkeypatch.setattr(reader, method, broken_second_page)
    assert await bench.drain() == 1
    assert await _status(bench, document_id) == ("error", "file_unreadable")
    assert await _jobs(bench) == []
    assert bench.recognizer.calls == []


async def test_io_failure_while_reading_file_stays_temporary(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await bench.portal.employee()
    document_id = await added(client, "заметка.txt", "текст заметки".encode())

    def unavailable(*_: Any) -> str:
        raise OSError("диск недоступен")

    monkeypatch.setattr(bench.reader, "page_text", unavailable)
    assert await bench.drain() == 1
    assert await _status(bench, document_id) == ("queued", None)
    assert [(job.attempts, job.locked_until) for job in await _jobs(bench)] == [(1, None)]
    monkeypatch.undo()
    await bench.portal.execute("UPDATE kb_jobs SET run_after = :now", now=bench.portal.clock.now())
    await bench.drain()
    assert await _status(bench, document_id) == ("ready", None)


async def test_text_volume_is_limited_by_configuration(settings: Settings, tmp_path: Path) -> None:
    tuned = kb_settings(
        settings,
        tmp_path,
        indexing=settings.kb.indexing.model_copy(update={"document_max_chars": 500}),
    )
    bench = await make_bench(await make_portal(tuned), tuned)
    try:
        client = await bench.portal.employee()
        fits = await added(client, "малый.txt", b"a" * 500)
        large = await added(client, "большой.txt", b"b " * 300)
        paged = await added(client, "страницы.pdf", samples.text_pdf(["x" * 200] * 3))
        await bench.drain()
        assert await _status(bench, fits) == ("ready", None)
        assert await _status(bench, large) == ("error", "document_too_long")
        assert await _status(bench, paged) == ("error", "document_too_long")
        assert await _jobs(bench) == []  # ошибка постоянная: задание снято сразу
        # Текст сверх предела в базу не попадает.
        total = await bench.portal.rows("SELECT sum(char_length(text)) AS chars FROM kb_pages")
        assert total[0].chars <= 500 + 400
    finally:
        await bench.qdrant.close()
        await close_portal(bench.portal)


# --- идемпотентность и повторы ---


async def test_temporary_failure_requeues_with_growing_pause(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "скан.pdf", samples.scan_pdf(4))
    bench.recognizer.failures = 100
    pauses = []
    for attempt in range(1, 7):
        assert await bench.drain() == 1
        assert await _status(bench, document_id) == ("queued", None)
        job = (await _jobs(bench))[0]
        assert (job.attempts, job.locked_until) == (attempt, None)
        pauses.append((job.run_after - portal.clock.now()).total_seconds())
        assert await bench.drain() == 0  # отложено: раньше срока задание не берётся
        portal.clock.advance(seconds=pauses[-1])
    assert pauses == [60, 120, 240, 480, 960, 1800]  # растёт вдвое до наибольшей паузы


async def test_attempts_run_out_into_error_by_cause(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    scan = await added(client, "скан.png", samples.png())
    bench.recognizer.failures = 100
    await _exhaust_attempts(bench)
    assert await _status(bench, scan) == ("error", "recognition_failed")
    assert await _jobs(bench) == []

    bench.recognizer.failures = 0
    text = await added(client, "текст.txt", "обычный текст".encode())
    bench.embedder.failures = 10_000
    await _exhaust_attempts(bench)
    assert await _status(bench, text) == ("error", "internal_error")
    # Сам по себе документ с ошибкой повторно не обрабатывается.
    bench.embedder.failures = 0
    assert await bench.drain() == 0
    # Автор возвращает его в очередь; счёт попыток начинается заново.
    assert (await client.post(f"/api/kb/documents/{text}/retry")).status_code == 200
    assert await bench.drain() == 1
    assert await _status(bench, text) == ("ready", None)


async def test_single_call_is_retried_inside_the_job(bench: KbBench) -> None:
    client = await bench.portal.employee()
    document_id = await added(client, "текст.txt", "текст документа".encode())
    bench.embedder.failures = bench.settings.worker.call_attempts - 1
    await bench.drain()
    assert await _status(bench, document_id) == ("ready", None)
    assert (await _jobs(bench)) == []


async def test_job_resumes_from_saved_pages_after_failure(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Страницы, прочитанные до сбоя, повторно не распознаются (§11.2)."""
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "скан.pdf", samples.scan_pdf(5))
    original = bench.recognizer.recognize
    seen: list[int] = []

    async def fail_on_second_batch(images: Any) -> list[str]:
        seen.append(len(images))
        if len(seen) == 2:
            raise VectorIndexUnavailableError  # любой временный сбой посреди документа
        return await original(images)

    monkeypatch.setattr(bench.recognizer, "recognize", fail_on_second_batch)
    await bench.drain()
    assert await _status(bench, document_id) == ("queued", None)
    saved = await portal.rows("SELECT number, text FROM kb_pages ORDER BY number")
    assert [page.number for page in saved] == [1, 2]
    assert (await portal.rows("SELECT pages_done FROM kb_documents"))[0].pages_done == 2

    portal.clock.advance(minutes=2)
    await bench.drain()
    assert await _status(bench, document_id) == ("ready", None)
    assert seen == [2, 2, 2, 1]  # после сбоя распознаны только страницы 3, 4 и 5
    after = await portal.rows("SELECT number, text FROM kb_pages ORDER BY number")
    assert [page.number for page in after] == [1, 2, 3, 4, 5]
    assert [page.text for page in after[:2]] == [page.text for page in saved]
    assert (await portal.rows("SELECT pages_done FROM kb_documents"))[0].pages_done == 5


async def test_repeated_indexing_creates_no_duplicates(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    await added(client, "договор.pdf", samples.text_pdf([samples.TEXT_LAYER, LONG_PAGE]))
    await added(client, "скан.png", samples.png())
    await bench.drain()
    fragments = await portal.rows("SELECT id FROM kb_fragments")
    points, recognized = await bench.points(), list(bench.recognizer.calls)

    assert await bench.reindexer.run(only_errors=False, recreate_collection=False) == 2
    statuses = await portal.rows("SELECT status FROM kb_documents")
    assert {row.status for row in statuses} == {"queued"}
    assert await bench.drain() == 2
    again = await portal.rows("SELECT id FROM kb_fragments")
    assert len(again) == len(fragments) == await bench.points() == points
    assert {row.id for row in again}.isdisjoint({row.id for row in fragments})  # заменены
    assert bench.recognizer.calls == recognized  # страницы повторно не распознавались
    assert len(await portal.rows("SELECT 1 FROM kb_pages")) == 3


async def test_abandoned_job_is_taken_again_after_lease_expires(bench: KbBench) -> None:
    """Воркер умер посреди задания: оно берётся заново по истечении аренды (§11.1)."""
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "текст.txt", "текст документа".encode())
    now = portal.clock.now()
    async with bench.uow() as uow:
        claimed = await uow.jobs.claim(now, now + timedelta(seconds=300))
    assert claimed is not None and claimed.attempts == 1
    assert await bench.drain() == 0  # пока аренда действует, задание не берётся
    portal.clock.advance(seconds=299)
    assert await bench.drain() == 0
    portal.clock.advance(seconds=2)
    assert await bench.drain() == 1
    assert await _status(bench, document_id) == ("ready", None)


async def test_document_of_crashed_worker_is_shown_queued_once_lease_is_over(
    bench: KbBench,
) -> None:
    """Воркер упал посреди задания: что видит пользователь до нового взятия.

    Пока аренда упавшего воркера действует, состояние остаётся `processing` — отличить
    падение от долгой работы нечем. Воркер, стартующий после истечения аренды,
    возвращает документ в `queued` ещё до того, как возьмёт задание.
    """
    portal = bench.portal
    client = await portal.employee()
    crashed = await added(client, "упавший.txt", "текст документа".encode())
    now = portal.clock.now()
    async with bench.uow() as uow:
        job = await uow.jobs.claim(now, now + timedelta(seconds=300))
        assert job is not None
        await uow.documents.set_status(job.document_id, "processing")
    portal.clock.advance(seconds=100)
    working = await added(client, "в работе.txt", "другой документ".encode())
    async with bench.uow() as uow:
        other = await uow.jobs.claim(
            portal.clock.now(), portal.clock.now() + timedelta(seconds=300)
        )
        assert other is not None
        await uow.documents.set_status(other.document_id, "processing")
    portal.clock.advance(seconds=201)  # аренда первого истекла, второго — ещё нет

    async with bench.uow() as uow:
        assert await uow.documents.requeue_abandoned(portal.clock.now()) == 1
    assert await _status(bench, crashed) == ("queued", None)
    assert await _status(bench, working) == ("processing", None)
    assert await bench.drain() == 1
    assert await _status(bench, crashed) == ("ready", None)


async def test_two_workers_never_take_the_same_job(bench: KbBench) -> None:
    client = await bench.portal.employee()
    for index in range(3):
        await added(client, f"документ {index}.txt", f"текст {index}".encode())
    now = bench.portal.clock.now()

    async def claim() -> UUID | None:
        async with bench.uow() as uow:
            job = await uow.jobs.claim(now, now + timedelta(seconds=300))
        return job.id if job else None

    taken = await asyncio.gather(*(claim() for _ in range(8)))
    job_ids = [job_id for job_id in taken if job_id is not None]
    assert len(job_ids) == len(set(job_ids)) == 3


async def test_delete_jobs_go_first_then_oldest(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    first = await added(client, "первый.txt", b"first")
    portal.clock.advance(seconds=1)
    await added(client, "второй.txt", b"second")
    portal.clock.advance(seconds=1)
    third = await added(client, "третий.txt", b"third")
    portal.clock.advance(seconds=1)
    await client.delete(f"/api/kb/documents/{third}")
    order = []
    now = portal.clock.now()
    for _ in range(3):
        async with bench.uow() as uow:
            job = await uow.jobs.claim(now, now + timedelta(seconds=300))
        assert job is not None
        order.append((job.kind, str(job.document_id)))
    assert order[0] == ("delete", third)
    assert order[1] == ("index", first)


# --- удаление ---


async def test_delete_removes_original_pages_fragments_and_vectors(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    keep = await added(client, "остаётся.txt", "этот документ остаётся".encode())
    gone = await added(client, "удаляется.pdf", samples.text_pdf([samples.TEXT_LAYER, LONG_PAGE]))
    await bench.drain()
    key = (await portal.rows("SELECT storage_key FROM kb_documents WHERE id = :id", id=gone))[
        0
    ].storage_key
    file = portal.container.settings.files.root / key
    kept_points = len(
        await portal.rows("SELECT 1 FROM kb_fragments WHERE document_id = :id", id=keep)
    )
    assert file.exists() and await bench.points() > kept_points

    assert (await client.delete(f"/api/kb/documents/{gone}")).status_code == 204
    assert await bench.drain() == 1
    assert not file.exists()
    for table in ("kb_documents", "kb_pages", "kb_fragments", "kb_jobs"):
        column = "id" if table == "kb_documents" else "document_id"
        assert await portal.rows(f"SELECT 1 FROM {table} WHERE {column} = :id", id=gone) == []
    assert await bench.points() == kept_points
    assert await _status(bench, keep) == ("ready", None)


async def test_delete_job_is_retried_without_limit_until_qdrant_answers(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "документ.txt", "текст".encode())
    await bench.drain()
    await client.delete(f"/api/kb/documents/{document_id}")
    key = (await portal.rows("SELECT storage_key FROM kb_documents"))[0].storage_key
    original = bench.index.delete_document

    async def unavailable(_: UUID) -> None:
        raise VectorIndexUnavailableError

    monkeypatch.setattr(bench.index, "delete_document", unavailable)
    for _ in range(bench.settings.worker.max_attempts + 3):
        assert await bench.drain() == 1
        portal.clock.advance(hours=1)
    job = (await _jobs(bench))[0]
    assert (job.kind, job.attempts) == ("delete", bench.settings.worker.max_attempts + 3)
    pause = (job.run_after - portal.clock.now()).total_seconds() + 3600
    assert pause == bench.settings.worker.retry_max_seconds
    # Пока точки не удалены, файл и строка на месте: порядок шагов — Qdrant → файл → строка.
    assert (portal.container.settings.files.root / key).exists()
    assert len(await portal.rows("SELECT 1 FROM kb_documents")) == 1

    monkeypatch.setattr(bench.index, "delete_document", original)
    assert await bench.drain() == 1
    assert await portal.rows("SELECT 1 FROM kb_documents") == []
    assert await bench.points() == 0


async def test_document_deleted_while_indexing_is_not_resurrected(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Удаление во время индексации: задание прекращается, документ стирается целиком."""
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "скан.pdf", samples.scan_pdf(4))
    original = bench.recognizer.recognize

    async def delete_meanwhile(images: Any) -> list[str]:
        if not bench.recognizer.calls:
            response = await client.delete(f"/api/kb/documents/{document_id}")
            assert response.status_code == 204
        return await original(images)

    monkeypatch.setattr(bench.recognizer, "recognize", delete_meanwhile)
    assert await bench.drain() == 2  # прерванная индексация и затем задание удаления
    assert bench.recognizer.calls == [2]  # оставшиеся страницы не распознавались
    assert await portal.rows("SELECT 1 FROM kb_documents") == []
    assert await portal.rows("SELECT 1 FROM kb_pages") == []
    assert await _jobs(bench) == []
    assert await bench.points() == 0
    assert list((portal.container.settings.files.root / "kb").iterdir()) == []


async def test_withdrawn_job_stops_before_next_recognition_batch(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Снятое задание не занимает модель до продления аренды: сверка — перед каждой пачкой.

    Задание снято без пометки документа удалённым, чтобы сработать могла только сверка
    самого задания, а не проверка документа при сохранении страницы.
    """
    portal = bench.portal
    client = await portal.employee()
    await added(client, "скан.pdf", samples.scan_pdf(6))
    original = bench.recognizer.recognize

    async def withdraw_meanwhile(images: Any) -> list[str]:
        texts = await original(images)
        await portal.execute("DELETE FROM kb_jobs")
        return texts

    monkeypatch.setattr(bench.recognizer, "recognize", withdraw_meanwhile)
    assert await bench.drain() == 1
    assert bench.recognizer.calls == [2]  # вторая и третья пачки к модели не ушли
    assert len(await portal.rows("SELECT 1 FROM kb_pages")) == 2
    assert await bench.points() == 0


async def test_large_text_file_is_not_decoded_whole_to_hit_the_limit(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Предел текста проверяется по началу файла: читается не больше нужного для него."""
    limit = 1000
    tuned = kb_settings(
        settings,
        tmp_path,
        indexing=settings.kb.indexing.model_copy(update={"document_max_chars": limit}),
    )
    bench = await make_bench(await make_portal(tuned), tuned)
    try:
        client = await bench.portal.employee()
        document_id = await added(client, "большой.txt", "строка текста\n".encode() * 400_000)
        decoded: list[int] = []
        original = text_module.decode_text

        def spy(data: bytes, **kwargs: Any) -> str | None:
            decoded.append(len(data))
            return original(data, **kwargs)

        monkeypatch.setattr(text_module, "decode_text", spy)
        await bench.drain()
        assert await _status(bench, document_id) == ("error", "document_too_long")
        assert decoded and max(decoded) <= (limit + 1) * 4 + 1  # из 9 МБ файла — единицы килобайт
        assert await bench.portal.rows("SELECT 1 FROM kb_pages") == []
    finally:
        await bench.qdrant.close()
        await close_portal(bench.portal)


async def test_delete_arriving_during_final_write_wins(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Блокировка строки документа: удаление ждёт конца записи и стирает её итог (§11.3)."""
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "документ.txt", "текст документа".encode())
    original = bench.index.replace_document
    deletion: list[asyncio.Task[Any]] = []

    async def replace(target: UUID, points: Any) -> None:
        deletion.append(asyncio.create_task(client.delete(f"/api/kb/documents/{document_id}")))
        await asyncio.sleep(0.2)  # удаление упирается в блокировку строки
        assert not deletion[0].done()
        await original(target, points)

    monkeypatch.setattr(bench.index, "replace_document", replace)
    assert await bench.worker.run_once() is True
    assert (await deletion[0]).status_code == 204
    assert await bench.points() == 1
    assert await bench.drain() == 1
    assert await bench.points() == 0
    assert await portal.rows("SELECT 1 FROM kb_documents") == []


# --- цикл воркера ---


async def _run_worker(worker: Worker, until: Any, timeout: float = 20.0) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        async with asyncio.timeout(timeout):
            while not await until():
                await asyncio.sleep(0.02)
    finally:
        stop.set()
        await task


def _fast_worker(bench: KbBench, **overrides: Any) -> Worker:
    settings = bench.settings.worker.model_copy(update={"poll_seconds": 0.05, **overrides})
    return Worker(bench.uow, bench.indexer, bench.index, bench.portal.clock, settings)


async def test_worker_loop_respects_concurrency_limit(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    for index in range(5):
        await added(client, f"скан {index}.png", samples.png(40 + index, 30))
    running = peak = 0
    original = bench.recognizer.recognize

    async def tracked(images: Any) -> list[str]:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return await original(images)

    monkeypatch.setattr(bench.recognizer, "recognize", tracked)

    async def all_ready() -> bool:
        rows = await portal.rows("SELECT status FROM kb_documents")
        return all(row.status == "ready" for row in rows)

    await _run_worker(_fast_worker(bench, concurrency=2), all_ready)
    assert peak == 2
    heartbeat = bench.settings.worker.heartbeat_file
    assert heartbeat_is_fresh(heartbeat, 60)


async def test_default_concurrency_is_one_job_at_a_time(bench: KbBench) -> None:
    assert bench.settings.worker.concurrency == 1


async def test_worker_stop_returns_job_to_queue_without_counting_attempt(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "скан.png", samples.png())
    started, hold = asyncio.Event(), asyncio.Event()

    async def hang(_: Any) -> list[str]:
        started.set()
        await hold.wait()
        return []

    monkeypatch.setattr(bench.recognizer, "recognize", hang)
    stop = asyncio.Event()
    task = asyncio.create_task(_fast_worker(bench).run(stop))
    await asyncio.wait_for(started.wait(), 10)
    assert await _status(bench, document_id) == ("processing", None)
    stop.set()
    await asyncio.wait_for(task, 10)
    job = (await _jobs(bench))[0]
    assert (job.attempts, job.locked_until) == (0, None)  # доступно следующему запуску сразу
    # Воркер остановлен — документ «в очереди», а не «обрабатывается» (§11.1).
    assert await _status(bench, document_id) == ("queued", None)

    monkeypatch.undo()
    assert await bench.drain() == 1
    assert await _status(bench, document_id) == ("ready", None)


async def test_lease_is_renewed_and_withdrawn_job_stops_working(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    await added(client, "скан.png", samples.png())
    started, hold = asyncio.Event(), asyncio.Event()

    async def hang(_: Any) -> list[str]:
        started.set()
        await hold.wait()
        return ["поздний текст"]

    monkeypatch.setattr(bench.recognizer, "recognize", hang)
    worker = _fast_worker(bench, lease_seconds=3, heartbeat_stale_seconds=60)
    stop = asyncio.Event()
    task = asyncio.create_task(worker.run(stop))
    try:
        await asyncio.wait_for(started.wait(), 10)
        first = (await _jobs(bench))[0].locked_until
        portal.clock.advance(seconds=2)
        await asyncio.sleep(1.3)  # аренда продлевается не реже раза в треть срока
        renewed = (await _jobs(bench))[0].locked_until
        assert renewed > first
        # Задание сняли (как при удалении документа): продление не находит строки.
        await portal.execute("DELETE FROM kb_jobs")
        await asyncio.sleep(1.3)
        hold.set()
        await asyncio.sleep(0.2)
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)
    assert await portal.rows("SELECT 1 FROM kb_pages") == []  # работа прекращена
    assert await _jobs(bench) == []


async def test_worker_does_not_start_on_collection_with_other_parameters(bench: KbBench) -> None:
    embeddings = bench.settings.embeddings.model_copy(update={"dimension": 768})
    other = bench.settings.model_copy(update={"embeddings": embeddings})
    worker = Worker(
        bench.uow,
        bench.indexer,
        QdrantVectorIndex(bench.qdrant, other),
        bench.portal.clock,
        bench.settings.worker,
    )
    with pytest.raises(CollectionMismatchError):
        await worker.run(asyncio.Event())


# --- portal reindex ---


async def test_reindex_only_errors_requeues_failed_documents(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    ready = await added(client, "готовый.txt", "готовый документ".encode())
    await bench.drain()
    bench.embedder.failures = 10_000
    failed = await added(client, "сбойный.txt", "документ со сбоем".encode())
    await _exhaust_attempts(bench)
    assert await _status(bench, failed) == ("error", "internal_error")
    deleted = await added(client, "удалённый.txt", "удалённый документ".encode())
    await client.delete(f"/api/kb/documents/{deleted}")

    bench.embedder.failures = 0
    assert await bench.reindexer.run(only_errors=True, recreate_collection=False) == 1
    assert await _status(bench, ready) == ("ready", None)
    assert await _status(bench, failed) == ("queued", None)
    jobs = await _jobs(bench)
    assert sorted((job.kind, job.attempts) for job in jobs) == [("delete", 0), ("index", 0)]
    await bench.drain()
    assert await _status(bench, failed) == ("ready", None)


async def test_reindex_resets_existing_job_instead_of_adding_second(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "скан.png", samples.png())
    bench.recognizer.failures = 3
    for _ in range(3):
        await bench.drain()
        portal.clock.advance(hours=1)
    portal.clock.advance(hours=-1)
    before = (await _jobs(bench))[0]
    assert before.attempts == 3 and before.run_after > portal.clock.now()

    assert await bench.reindexer.run(only_errors=False, recreate_collection=False) == 1
    jobs = await _jobs(bench)
    assert len(jobs) == 1
    assert (jobs[0].attempts, jobs[0].run_after) == (0, portal.clock.now())
    assert await bench.drain() == 1
    assert await _status(bench, document_id) == ("ready", None)


async def test_reindex_recreates_collection_only_when_worker_is_idle(bench: KbBench) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "документ.txt", "текст документа".encode())
    await bench.drain()
    busy = await added(client, "в работе.txt", "другой текст".encode())
    now = portal.clock.now()
    async with bench.uow() as uow:
        assert await uow.jobs.claim(now, now + timedelta(seconds=300)) is not None
    with pytest.raises(WorkerRunningError):
        await bench.reindexer.run(only_errors=False, recreate_collection=True)
    assert await bench.points() == 1  # коллекция не тронута

    portal.clock.advance(seconds=301)  # аренда истекла: цикл воркера остановлен
    assert await bench.reindexer.run(only_errors=False, recreate_collection=True) == 2
    assert await bench.points() == 0
    assert await bench.drain() == 2
    assert await bench.points() == 2
    assert await _status(bench, document_id) == ("ready", None)
    assert await _status(bench, busy) == ("ready", None)
