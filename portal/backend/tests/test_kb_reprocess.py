"""Очередь и переобработка: замена индекса без выпадения из поиска, приоритеты, пауза (§11)."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from portal.core.settings import Settings
from portal.kb.ports import KnowledgeSelection, VectorIndexUnavailableError
from tests import samples
from tests.conftest import close_portal, make_kb_bench
from tests.kb_support import SHARED, KbBench, added, kb_settings

pytestmark = pytest.mark.anyio

TEXT = "Договор аренды земельного участка 77:01:0004012:345 сроком на сорок девять лет."
QUERY = "договор аренды участка 77:01:0004012:345"


@pytest.fixture
async def explaining(settings: Settings, tmp_path: Path) -> AsyncIterator[KbBench]:
    """Стенд с включённым заданием `context`."""
    built = await make_kb_bench(kb_settings(settings, tmp_path, context={"enabled": True}))
    yield built
    await close_portal(built.portal)


async def _legacy_document(
    bench: KbBench,
    client: httpx.AsyncClient,
    name: str = "договор.txt",
    content: bytes = TEXT.encode(),
) -> str:
    """Готовый документ, каким он остался от версии до 0.15: `index_version = 1`."""
    document_id = await added(client, name, content)
    await bench.drain()
    await bench.portal.execute("UPDATE kb_documents SET index_version = 1")
    return document_id


async def _fragment_ids(bench: KbBench, document_id: str) -> set[UUID]:
    rows = await bench.portal.rows(
        "SELECT id FROM kb_fragments WHERE document_id = :id", id=document_id
    )
    return {row.id for row in rows}


async def _found(bench: KbBench, user: UUID, query: str = QUERY) -> set[UUID]:
    """Фрагменты, которые поиск отдаёт пользователю прямо сейчас."""
    return {
        source.fragment_id
        for source in (await bench.knowledge.retrieve(query, user, SHARED)).sources
    }


async def _document(bench: KbBench, document_id: str) -> Any:
    rows = await bench.portal.rows(
        "SELECT status, index_version, reprocess_error FROM kb_documents WHERE id = :id",
        id=document_id,
    )
    return tuple(rows[0])


async def _jobs(bench: KbBench) -> list[tuple[str, int]]:
    rows = await bench.portal.rows("SELECT kind, priority FROM kb_jobs ORDER BY priority, kind")
    return [(row.kind, row.priority) for row in rows]


async def _expire_retry_pauses(bench: KbBench) -> None:
    await bench.portal.execute("UPDATE kb_jobs SET run_after = :now", now=bench.portal.clock.now())


async def test_document_is_found_at_every_step_of_reprocessing(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Прежний индекс действует до фиксации завершения, новый — сразу после неё (§10.6)."""
    portal = bench.portal
    client = await portal.employee()
    user = await portal.user_id("ivanov")
    document_id = await _legacy_document(bench, client, "скан.png", samples.png())
    await client.put(f"/api/kb/documents/{document_id}/tags", json={"tags": ["Договоры"]})
    old = await _fragment_ids(bench, document_id)
    text = (await portal.rows("SELECT text FROM kb_pages"))[0].text
    recognized = list(bench.recognizer.calls)
    assert await bench.reprocessor.start(all_ready=False) == 1
    assert await bench.reprocessor.start(all_ready=False) == 0  # второе задание не создаётся

    seen: list[tuple[str, set[UUID]]] = []

    def probed(step: str) -> None:
        original = getattr(bench.index, step)

        async def call(*args: Any) -> None:
            seen.append((f"до {step}", await _found(bench, user, text)))
            await original(*args)
            seen.append((f"после {step}", await _found(bench, user, text)))
            assert (await _document(bench, document_id))[0] == "ready"

        monkeypatch.setattr(bench.index, step, call)

    for step in ("prune_document", "stage_document", "publish_document"):
        probed(step)
    assert await bench.drain() == 1

    new = await _fragment_ids(bench, document_id)
    assert new and new.isdisjoint(old)
    # Уборка — дважды: перед запасом и после фиксации. До фиксации находится только прежнее.
    steps = ["prune_document", "stage_document", "publish_document", "prune_document"]
    assert [step for step, _ in seen] == [
        f"{when} {step}" for step in steps for when in ("до", "после")
    ]
    assert all(found == old for _, found in seen[:6])
    assert all(found == new for _, found in seen[6:])
    assert await _document(bench, document_id) == ("ready", 2, None)
    assert await bench.points() == len(new)
    assert bench.recognizer.calls == recognized  # страницы заново не распознаются
    # Теги из базы стоят и на новых точках.
    by_tag = KnowledgeSelection(frozenset({"shared"}), frozenset({"договоры"}))
    found = await bench.knowledge.retrieve(text, user, by_tag)
    assert {source.fragment_id for source in found.sources} == new
    assert await bench.reprocessor.start(all_ready=False) == 0
    assert await bench.reprocessor.start(all_ready=True) == 1


@pytest.mark.parametrize("step", ["stage_document", "publish_document"])
async def test_failure_in_the_middle_keeps_the_old_index_and_marks_the_document(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """Сбой до фиксации: документ ищется в прежнем виде, запас убран, отметка о сбое (§11.6)."""
    portal = bench.portal
    client = await portal.employee()
    user = await portal.user_id("ivanov")
    document_id = await _legacy_document(bench, client)
    old = await _fragment_ids(bench, document_id)
    await bench.reprocessor.start(all_ready=False)
    original = getattr(bench.index, step)

    async def broken(*args: Any) -> None:
        await original(*args)  # точки записаны или уже введены в действие — и тут сбой
        raise VectorIndexUnavailableError

    monkeypatch.setattr(bench.index, step, broken)
    for _ in range(bench.settings.worker.max_attempts):
        assert await bench.drain() == 1
        assert await _found(bench, user) == old
        assert await bench.points() == len(old)  # лишних точек после попытки нет
        assert (await _document(bench, document_id))[0] == "ready"
        await _expire_retry_pauses(bench)

    assert await _document(bench, document_id) == ("ready", 1, "internal_error")
    assert await _jobs(bench) == [] and await _fragment_ids(bench, document_id) == old
    shown = (await client.get(f"/api/kb/documents/{document_id}")).json()
    assert (shown["status"], shown["reprocess_error"], shown["reprocessing"]) == (
        "ready", "internal_error", False,
    )  # fmt: skip

    # Повтор по запросу автора: отметка снята сразу, задание — с приоритетом новой загрузки.
    await bench.reprocessor.start(all_ready=False)
    assert await _jobs(bench) == [("reprocess", 40)]
    retried = await client.post(f"/api/kb/documents/{document_id}/retry")
    assert retried.status_code == 200
    assert (retried.json()["reprocess_error"], retried.json()["reprocessing"]) == (None, True)
    assert await _jobs(bench) == [("reprocess", 20)]
    again = await client.post(f"/api/kb/documents/{document_id}/retry")
    assert (again.status_code, again.json()["error"]["code"]) == (409, "document_not_in_error")

    monkeypatch.setattr(bench.index, step, original)
    assert await bench.drain() == 1
    assert await _document(bench, document_id) == ("ready", 2, None)
    new = await _fragment_ids(bench, document_id)
    assert new.isdisjoint(old) and await _found(bench, user) == new
    assert await bench.points() == len(new)


async def test_new_upload_overtakes_bulk_reprocessing_and_pause_holds_only_bulk(
    explaining: KbBench,
) -> None:
    """Приоритет действует на границе заданий; пауза касается только массовой переобработки."""
    bench = explaining
    portal = bench.portal
    client = await portal.employee()
    for n in range(3):
        await added(client, f"старый {n}.txt", f"{TEXT} Экземпляр {n}.".encode())
    await bench.drain()
    await portal.execute("UPDATE kb_documents SET index_version = 1")
    assert await bench.reprocessor.start(all_ready=False) == 3

    # Первый документ переобработан; его пояснения — раньше следующего документа.
    assert await bench.worker.run_once()
    assert await _jobs(bench) == [("context", 35), ("reprocess", 40), ("reprocess", 40)]
    fresh = await added(client, "новый.txt", "Совсем новый приказ о кадастровой оценке.".encode())
    assert await bench.worker.run_once()
    assert (await _document(bench, fresh))[0] == "ready"  # обогнал и пояснения, и переобработку
    assert await _jobs(bench) == [
        ("context", 30), ("context", 35), ("reprocess", 40), ("reprocess", 40),
    ]  # fmt: skip

    await bench.reprocessor.set_paused(True)
    await bench.drain()  # пояснения нового документа: под паузу не попадают
    bulk = [("context", 35), ("reprocess", 40), ("reprocess", 40)]
    assert await _jobs(bench) == bulk  # массовые задания ждут
    during_pause = await added(client, "ещё.txt", "Документ, загруженный во время паузы.".encode())
    await bench.drain()
    assert (await _document(bench, during_pause))[0] == "ready"
    assert await _jobs(bench) == bulk
    # Переобработка по запросу пользователя под паузу не попадает.
    stuck = (await portal.rows("SELECT document_id FROM kb_jobs WHERE kind = 'reprocess'"))[0]
    await portal.execute(
        "UPDATE kb_documents SET reprocess_error = 'internal_error' WHERE id = :id",
        id=stuck.document_id,
    )
    assert (await client.post(f"/api/kb/documents/{stuck.document_id}/retry")).status_code == 200
    await bench.drain()
    assert await _document(bench, str(stuck.document_id)) == ("ready", 2, None)
    assert await _jobs(bench) == bulk[:2]

    status = await bench.reprocessor.status()
    assert (status.current, status.reprocess_jobs, status.context_jobs, status.paused) == (
        4, 1, 1, True,
    )  # fmt: skip
    await bench.reprocessor.set_paused(False)
    await bench.drain()
    status = await bench.reprocessor.status()
    assert (status.current, status.reprocess_jobs, status.context_jobs, status.paused) == (
        5, 0, 0, False,
    )  # fmt: skip


async def test_job_that_ran_out_of_attempts_before_claim_is_not_executed(bench: KbBench) -> None:
    """Процесс воркера погибал на задании: оно не берётся снова и снова (§11.1)."""
    portal = bench.portal
    client = await portal.employee()
    user = await portal.user_id("ivanov")
    reprocessed = await _legacy_document(bench, client)
    old = await _fragment_ids(bench, reprocessed)
    await bench.reprocessor.start(all_ready=False)
    indexed = await added(client, "новый.txt", "Новый документ.".encode())
    batches = list(bench.embedder.batches)
    # Аренда погибшего воркера истекла, счётчик попыток уже на пределе.
    await portal.execute(
        "UPDATE kb_jobs SET attempts = :attempts, locked_until = :expired",
        attempts=bench.settings.worker.max_attempts,
        expired=portal.clock.now(),
    )
    await portal.execute("UPDATE kb_documents SET status = 'processing' WHERE id = :id", id=indexed)

    assert await bench.drain() == 2
    assert bench.embedder.batches == batches  # работа не выполнялась
    assert await _jobs(bench) == []
    assert await _document(bench, indexed) == ("error", 1, None)
    row = await portal.rows("SELECT error_code FROM kb_documents WHERE id = :id", id=indexed)
    assert row[0].error_code == "internal_error"
    assert await _document(bench, reprocessed) == ("ready", 1, "internal_error")
    assert await _found(bench, user) == old


async def test_reindex_during_reprocessing_withdraws_it_and_waits_for_it_to_stop(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Один документ — одно задание обработки: `index` ждёт снятого `reprocess` (§11.1, §13.4)."""
    portal = bench.portal
    client = await portal.employee()
    document_id = await _legacy_document(bench, client)
    old = await _fragment_ids(bench, document_id)
    await bench.reprocessor.start(all_ready=False)
    gate, reached = asyncio.Event(), asyncio.Event()
    original = bench.embedder.embed_documents

    async def gated(texts: Any) -> list[list[float]]:
        reached.set()
        await gate.wait()
        return await original(texts)

    monkeypatch.setattr(bench.embedder, "embed_documents", gated)
    reprocessing = asyncio.create_task(bench.worker.run_once())
    await reached.wait()
    assert await bench.reindexer.run(only_errors=False, recreate_collection=False) == 1
    assert await _jobs(bench) == [("index", 20)]
    assert await bench.worker.run_once() is False  # документ ещё занят снятым заданием
    gate.set()
    assert await reprocessing
    # Снятое задание индекс не заменило и запаса не оставило.
    assert await _fragment_ids(bench, document_id) == old and await bench.points() == len(old)
    assert (await _document(bench, document_id))[0] == "queued"
    assert await bench.drain() == 1
    assert await _document(bench, document_id) == ("ready", 2, None)


async def test_reprocess_of_a_document_that_is_not_ready_is_dropped_without_a_mark(
    bench: KbBench,
) -> None:
    portal = bench.portal
    client = await portal.employee()
    document_id = await _legacy_document(bench, client)
    await bench.reprocessor.start(all_ready=False)
    await portal.execute("UPDATE kb_documents SET status = 'error', error_code = 'no_text'")
    assert await bench.drain() == 1
    assert await _jobs(bench) == []
    assert await _document(bench, document_id) == ("error", 1, None)


async def test_pictures_keep_their_identifiers_after_reprocessing(bench: KbBench) -> None:
    """Картинка с тем же содержимым открывается из прежних ответов, как раньше (§8.6)."""
    portal = bench.portal
    client = await portal.employee()
    content = samples.docx_with_pictures(
        samples.png(300, 200, "red"), samples.png(320, 240, "blue")
    )
    document_id = await added(client, "схема.docx", content)
    await bench.drain()
    images = await portal.rows(
        "SELECT id, description, storage_key FROM kb_images ORDER BY ordinal"
    )
    asked = len(bench.model.prompts)
    old = await _fragment_ids(bench, document_id)

    assert await bench.reprocessor.start(all_ready=True) == 1
    assert await bench.drain() == 1
    assert (
        await portal.rows("SELECT id, description, storage_key FROM kb_images ORDER BY ordinal")
        == images
    )
    assert len(bench.model.prompts) == asked  # повторно не описывались
    linked = await portal.rows(
        "SELECT id, image_id FROM kb_fragments WHERE image_id IS NOT NULL ORDER BY ordinal"
    )
    assert [row.image_id for row in linked] == [image.id for image in images]
    assert {row.id for row in linked}.isdisjoint(old)
    served = await client.get(f"/api/kb/documents/{document_id}/images/{images[0].id}")
    assert served.status_code == 200


async def test_stopping_the_worker_does_not_interrupt_cleanup_after_replacement(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Остановка между фиксацией и уборкой: документ готов, прежние точки убраны (§10.6, 2.4)."""
    portal = bench.portal
    client = await portal.employee()
    document_id = await added(client, "договор.txt", TEXT.encode())
    await bench.drain()
    assert await bench.reindexer.run(only_errors=False, recreate_collection=False) == 1
    gate, reached = asyncio.Event(), asyncio.Event()
    original = bench.index.prune_document
    calls = 0

    async def gated(*args: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:  # уборка после фиксации
            reached.set()
            await gate.wait()
        await original(*args)

    monkeypatch.setattr(bench.index, "prune_document", gated)
    running = asyncio.create_task(bench.worker.run_once())
    await reached.wait()
    running.cancel()
    await asyncio.sleep(0.05)
    assert not running.done()  # уборка доводится до конца
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await _document(bench, document_id) == ("ready", 2, None)
    assert await bench.points() == len(await _fragment_ids(bench, document_id))
    assert await _jobs(bench) == []


CONTEXT_PROMPT = "Поясни место фрагмента в документе."
PARAGRAPHS = "\n\n".join(
    " ".join(f"Пункт {n}.{k} положения о кадастровой оценке участков." for k in range(12))
    for n in range(1, 4)
)
_FRAGMENTS = """
SELECT f.context, substr(p.text, f.start_offset + 1, f.end_offset - f.start_offset) AS text
FROM kb_fragments f JOIN kb_pages p ON p.document_id = f.document_id AND p.number = f.page_number
WHERE f.image_id IS NULL ORDER BY f.ordinal
"""


def _explanations_asked(bench: KbBench) -> int:
    return sum(prompt.startswith(CONTEXT_PROMPT) for prompt in bench.model.prompts)


async def test_reprocessing_carries_explanations_of_unchanged_fragments(
    explaining: KbBench,
) -> None:
    """Перенос пояснений (§11.2): тот же раздел и текст — пояснение без модели."""
    bench = explaining
    portal = bench.portal
    client = await portal.employee()
    await added(client, "положение.txt", PARAGRAPHS.encode())
    await bench.drain()
    before = await portal.rows(_FRAGMENTS)
    assert len(before) >= 3 and all(row.context for row in before)
    # Пустое пояснение тоже переносится; изменённый последний фрагмент — нет.
    await portal.execute(
        "UPDATE kb_fragments SET context = '' WHERE ordinal = 0 AND image_id IS NULL"
    )
    await portal.execute("UPDATE kb_pages SET text = text || ' Дополнение.'")
    asked = _explanations_asked(bench)

    assert await bench.reprocessor.start(all_ready=True) == 1
    assert await bench.worker.run_once()  # reprocess
    after = await portal.rows(_FRAGMENTS)
    assert after[0].context == "" and after[-1].text.endswith("Дополнение.")
    assert [row.context for row in after[1:-1]] == [row.context for row in before[1:-1]]
    assert after[-1].context is None
    await bench.drain()
    assert _explanations_asked(bench) == asked + 1  # только изменённый
    assert await _jobs(bench) == []


async def test_explanations_are_not_carried_when_the_stage_is_off(bench: KbBench) -> None:
    """Флаг выключен: индекс строится без пояснений, переобработка убирает их (§11.2)."""
    portal = bench.portal
    client = await portal.employee()
    await added(client, "положение.txt", PARAGRAPHS.encode())
    await bench.drain()
    await portal.execute("UPDATE kb_fragments SET context = 'Пояснение прежней сборки'")
    assert await bench.reprocessor.start(all_ready=True) == 1
    await bench.drain()
    contexts = [row.context for row in await portal.rows(_FRAGMENTS)]
    assert contexts and set(contexts) == {None}
    assert await _jobs(bench) == []
