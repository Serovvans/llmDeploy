"""Уступка основного места длинным заданием: квант, приоритеты, пауза (docs/portal-api.md §11.1)."""

import asyncio
import logging
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from portal.kb.domain import Job
from portal.kb.outcomes import JobYieldedError
from portal.kb.repositories import SqlJobQueue
from portal.kb.slicing import JobSlice
from tests import samples
from tests.kb_support import KbBench, added

pytestmark = pytest.mark.anyio


async def _job(bench: KbBench, document_id: str) -> Any:
    rows = await bench.portal.rows(
        "SELECT kind, priority, attempts, locked_until, created_at FROM kb_jobs"
        " WHERE document_id = :id",
        id=document_id,
    )
    return rows[0]


async def _status(bench: KbBench, client: Any, document_id: str) -> tuple[str, Any]:
    shown = (await client.get(f"/api/kb/documents/{document_id}")).json()
    return shown["status"], shown["progress"]


def _slow_recognition(bench: KbBench, monkeypatch: pytest.MonkeyPatch) -> None:
    """Каждая пачка распознавания длится квант."""
    original = bench.recognizer.recognize

    async def slow(images: Any) -> list[str]:
        bench.portal.clock.advance(seconds=bench.settings.worker.slice_seconds)
        return await original(images)

    monkeypatch.setattr(bench.recognizer, "recognize", slow)


async def test_slice_counts_from_first_batch_and_respects_priorities(bench: KbBench) -> None:
    """Когда пора уступать: квант с начала первой пачки, ждущий не ниже приоритетом."""
    portal = bench.portal
    clock = portal.clock
    client = await portal.employee()
    running = await added(client, "длинный.pdf", samples.scan_pdf(2))
    waiting = await added(client, "короткий.txt", "Короткий приказ.".encode())
    async with bench.uow() as uow:
        claimed = await uow.jobs.claim(clock.now(), clock.now() + timedelta(seconds=300))
    assert claimed is not None and str(claimed.document_id) == running
    busy = {claimed.document_id}

    def slice_of(job: Job) -> JobSlice:
        return JobSlice(job, bench.uow, clock, bench.settings.worker.slice_seconds, busy)

    async def yields(turn: JobSlice) -> bool:
        try:
            await turn.check()
        except JobYieldedError:
            return True
        return False

    async def wait_as(**values: Any) -> None:
        assignments = ", ".join(f"{name} = :{name}" for name in values)
        await portal.execute(
            f"UPDATE kb_jobs SET {assignments} WHERE document_id = :id", id=waiting, **values
        )

    turn = slice_of(claimed)
    clock.advance(seconds=1000)  # работа до первой пачки в квант не входит
    assert not await yields(turn)
    turn.start()
    clock.advance(seconds=299)
    assert not await yields(turn)
    clock.advance(seconds=1)
    assert await yields(turn)  # ждёт новая загрузка того же приоритета

    await wait_as(priority=30)  # пояснения ниже приоритетом — не повод
    assert not await yields(turn)
    await wait_as(priority=20, run_after=clock.now() + timedelta(seconds=60))
    assert not await yields(turn)  # отложено до повтора
    await wait_as(run_after=clock.now(), locked_until=clock.now() + timedelta(seconds=60))
    assert not await yields(turn)  # уже выполняется
    await wait_as(locked_until=None)
    busy.add(UUID(waiting))  # его документ уже обрабатывается — место он не взял бы
    assert not await yields(turn)
    busy.discard(UUID(waiting))
    assert await yields(turn)

    # Массовая переобработка уступает только обычным заданиям, а на паузе — сразу.
    bulk = slice_of(replace(claimed, kind="reprocess", priority=40))
    bulk.start()
    clock.advance(seconds=300)
    await wait_as(priority=35)
    assert not await yields(bulk)
    await wait_as(priority=30)
    assert await yields(bulk)
    await wait_as(priority=40)
    fresh = slice_of(replace(claimed, kind="reprocess", priority=40))
    fresh.start()
    assert not await yields(fresh)
    await bench.reprocessor.set_paused(True)
    try:
        assert await yields(fresh)
        assert not await yields(slice_of(claimed))  # пауза держит только массовые
    finally:
        await bench.reprocessor.set_paused(False)  # строка паузы переживает тест


async def test_long_index_yields_to_waiting_upload_and_resumes(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    portal = bench.portal
    client = await portal.employee()
    long = await added(client, "длинный скан.pdf", samples.scan_pdf(4))
    portal.clock.advance(seconds=1)
    short = await added(client, "короткий.txt", "Короткий приказ.".encode())
    short_created = (await _job(bench, short)).created_at
    _slow_recognition(bench, monkeypatch)

    with caplog.at_level(logging.INFO, logger="portal.worker.loop"):
        assert await bench.worker.run_once()
    assert "job yielded" in [record.getMessage() for record in caplog.records]
    yielded = await _job(bench, long)
    assert (yielded.attempts, yielded.locked_until) == (0, None)
    assert yielded.created_at > short_created  # встал за ждущими своего приоритета
    assert await _status(bench, client, long) == ("queued", None)
    assert bench.recognizer.calls == [2]

    assert await bench.worker.run_once()
    assert (await _status(bench, client, short))[0] == "ready"
    assert (await _status(bench, client, long))[0] == "queued"
    # Длинный продолжает с места остановки; ждущих нет — доводит до конца.
    assert await bench.worker.run_once()
    assert (await _status(bench, client, long))[0] == "ready"
    assert bench.recognizer.calls == [2, 2]
    assert await portal.rows("SELECT 1 FROM kb_jobs") == []


async def test_paused_bulk_reprocessing_yields_after_portion_and_keeps_its_place(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    portal = bench.portal
    client = await portal.employee()
    pictures = (samples.png(300, 200, color) for color in ("red", "green", "blue"))
    document_id = await added(client, "схемы.docx", samples.docx_with_pictures(*pictures))
    await bench.drain()
    await portal.execute("UPDATE kb_images SET description_version = 1")
    assert await bench.reprocessor.start(all_ready=False, images=True) == 1
    created = (await _job(bench, document_id)).created_at
    original = bench.model.complete_with_reason

    async def pause_on_first(request: Any) -> Any:
        await bench.reprocessor.set_paused(True)
        return await original(request)

    monkeypatch.setattr(bench.model, "complete_with_reason", pause_on_first)
    asked = len(bench.model.max_tokens)
    assert await bench.worker.run_once()

    # Порция из двух описаний сохранена; задание уступило место, не дожидаясь кванта.
    job = await _job(bench, document_id)
    assert (job.kind, job.attempts, job.locked_until, job.created_at) == (
        "reprocess", 0, None, created,
    )  # fmt: skip
    pending = await portal.rows("SELECT 1 FROM kb_images WHERE pending_description IS NOT NULL")
    assert len(pending) == bench.settings.worker.background_llm_parallel == 2
    shown = (await client.get(f"/api/kb/documents/{document_id}")).json()
    assert (shown["status"], shown["reprocessing"]) == ("ready", False)
    assert not await bench.worker.run_once()  # пауза держит массовую переобработку

    monkeypatch.setattr(bench.model, "complete_with_reason", original)
    await bench.reprocessor.set_paused(False)
    await bench.drain()
    assert len(bench.model.max_tokens) == asked + 3  # каждая картинка описана один раз
    rows = await portal.rows("SELECT description_version, pending_description FROM kb_images")
    assert [tuple(row) for row in rows] == [(2, None)] * 3


async def test_yield_waits_for_renewal_in_flight(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Перед уступкой продление аренды останавливается и дожидается идущего (§11.1).

    Иначе продление, зафиксированное после уступки, вернуло бы аренду, и задание было
    бы невидимо до её истечения.
    """
    portal = bench.portal
    client = await portal.employee()
    long = await added(client, "длинный скан.pdf", samples.scan_pdf(2))
    portal.clock.advance(seconds=1)
    await added(client, "короткий.txt", "Короткий приказ.".encode())
    renewing, release = asyncio.Event(), asyncio.Event()
    renewed: list[bool] = []
    original_renew = SqlJobQueue.renew

    async def held(self: SqlJobQueue, job_id: UUID, lease_until: Any) -> bool:
        renewing.set()
        await release.wait()
        kept = await original_renew(self, job_id, lease_until)
        renewed.append(kept)
        return kept

    original = bench.recognizer.recognize

    async def recognize_while_renewing(images: Any) -> list[str]:
        await renewing.wait()
        portal.clock.advance(seconds=bench.settings.worker.slice_seconds)
        asyncio.get_running_loop().call_later(0.2, release.set)
        return await original(images)

    monkeypatch.setattr(SqlJobQueue, "renew", held)
    monkeypatch.setattr(bench.recognizer, "recognize", recognize_while_renewing)
    settings = bench.settings.worker.model_copy(update={"lease_seconds": 3})
    assert await asyncio.wait_for(bench.make_worker(settings=settings).run_once(), 10)

    assert renewed == [True]  # продление дошло до конца, а не оборвано
    job = await _job(bench, long)
    assert (job.attempts, job.locked_until) == (0, None)
