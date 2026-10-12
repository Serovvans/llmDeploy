"""Команды базы знаний: `worker-health`, `reindex`, `reprocess`, `eval-search` (§13.4)."""

import asyncio
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from llm_stub.app import create_app as create_stub
from qdrant_client import AsyncQdrantClient

from portal import cli
from portal.core import settings as settings_module
from portal.core.container import build_worker_container
from portal.core.settings import ConfigError, Settings, load_settings
from portal.kb.qdrant import QdrantVectorIndex
from tests.conftest import CONFIG_DIR
from tests.kb_support import KbBench, added, kb_settings
from tests.support import live_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def command_line(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], override_path: Path, tmp_path: Path
) -> Iterator[Settings]:
    """Окружение команды: настройки тестовой базы и отметка воркера во временном каталоге."""
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    tuned = kb_settings(settings_module.load_settings(environ, CONFIG_DIR, override_path), tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda env: tuned)
    yield tuned


def _local_worker(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> AsyncQdrantClient:
    """Команда получает воркер на локальном Qdrant вместо сервера."""
    qdrant = AsyncQdrantClient(location=":memory:")
    monkeypatch.setattr(
        cli, "build_worker_container", lambda _: build_worker_container(settings, qdrant=qdrant)
    )
    return qdrant


async def _in_thread(*argv: str) -> int:
    return await asyncio.to_thread(cli.main, argv)


async def test_worker_health_needs_fresh_heartbeat_and_database(command_line: Settings) -> None:
    heartbeat = command_line.kb.worker.heartbeat_file
    assert await _in_thread("worker-health") == 1  # цикл ещё не работал
    heartbeat.touch()
    assert await _in_thread("worker-health") == 0
    stale = time.time() - command_line.kb.worker.heartbeat_stale_seconds - 5
    os.utime(heartbeat, (stale, stale))
    assert await _in_thread("worker-health") == 1


async def test_worker_health_fails_when_database_is_down(
    command_line: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    command_line.kb.worker.heartbeat_file.touch()
    database = command_line.database.model_copy(update={"host": "127.0.0.1", "port": 9})
    broken = command_line.model_copy(update={"database": database})
    monkeypatch.setattr(cli, "load_settings", lambda env: broken)
    assert await _in_thread("worker-health") == 1


async def test_worker_health_requires_only_database_password(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], override_path: Path, tmp_path: Path
) -> None:
    only_database = {"PORTAL_DB_PASSWORD": environ["PORTAL_DB_PASSWORD"]}
    tuned = kb_settings(load_settings(only_database, CONFIG_DIR, override_path), tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda env: tuned)
    tuned.kb.worker.heartbeat_file.touch()
    assert await _in_thread("worker-health") == 0


@pytest.mark.parametrize("variable", ["PORTAL_LLM_API_KEY", "QDRANT_API_KEY"])
def test_kb_commands_refuse_to_start_without_their_variables(
    environ: dict[str, str], override_path: Path, variable: str
) -> None:
    """`worker`, `reindex` и `eval-search` собирают одни и те же зависимости (§13.5)."""
    incomplete = {name: value for name, value in environ.items() if name != variable}
    settings = load_settings(incomplete, CONFIG_DIR, override_path)
    with pytest.raises(ConfigError, match=variable):
        build_worker_container(settings)


async def test_kb_commands_do_not_need_model_context_or_secret_key(
    environ: dict[str, str], override_path: Path
) -> None:
    needed = ("PORTAL_DB_PASSWORD", "PORTAL_LLM_API_KEY", "QDRANT_API_KEY")
    settings = load_settings({name: environ[name] for name in needed}, CONFIG_DIR, override_path)
    await build_worker_container(settings).aclose()


async def test_reindex_command_requeues_documents(
    bench: KbBench, command_line: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    client = await bench.portal.employee()
    ready = await added(client, "готовый.txt", "готовый документ".encode())
    await bench.drain()
    failed = await added(client, "пустой.txt", b"  ")
    await bench.drain()

    _local_worker(monkeypatch, command_line)
    assert await _in_thread("reindex", "--only-errors") == 0
    assert capsys.readouterr().out == "Документов возвращено в очередь: 1\n"
    statuses = {
        str(row.id): row.status
        for row in await bench.portal.rows("SELECT id, status FROM kb_documents")
    }
    assert statuses == {ready: "ready", failed: "queued"}

    _local_worker(monkeypatch, command_line)
    assert await _in_thread("reindex") == 0
    assert capsys.readouterr().out == "Документов возвращено в очередь: 2\n"
    jobs = await bench.portal.rows("SELECT kind, attempts FROM kb_jobs")
    assert [(job.kind, job.attempts) for job in jobs] == [("index", 0), ("index", 0)]


async def test_reprocess_commands_need_only_database_and_print_only_numbers(
    bench: KbBench, monkeypatch: pytest.MonkeyPatch, environ: dict[str, str],
    override_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """Команды `portal reprocess` работают без ключей модели и Qdrant (§13.5)."""
    client = await bench.portal.employee()
    await added(client, "секретный договор.txt", "готовый документ".encode())
    await bench.drain()
    await bench.portal.execute(
        "UPDATE kb_documents SET index_version = 1, reprocess_error = 'recognition_failed'"
    )
    only_database = {"PORTAL_DB_PASSWORD": environ["PORTAL_DB_PASSWORD"]}
    tuned = kb_settings(load_settings(only_database, CONFIG_DIR, override_path), tmp_path)
    monkeypatch.setattr(cli, "load_settings", lambda env: tuned)

    assert await _in_thread("reprocess", "start") == 0
    assert capsys.readouterr().out == "Документов поставлено на переобработку: 1\n"
    assert await _in_thread("reprocess", "start", "--all") == 0
    assert capsys.readouterr().out == "Документов поставлено на переобработку: 0\n"
    assert await _in_thread("reprocess", "pause") == 0
    capsys.readouterr()
    assert await _in_thread("reprocess", "status") == 0
    assert capsys.readouterr().out == (
        "Документов с текущим индексом: 0\n"
        "Заданий переобработки в очереди: 1\n"
        "Заданий пояснений в очереди: 0\n"
        "Документов со сбоем переобработки: 1\n"
        "  recognition_failed: 1\n"
        "Пауза: да\n"
    )
    # Команда ставит задание по системным часам, стенд живёт по своим.
    await bench.portal.execute("UPDATE kb_jobs SET run_after = :now", now=bench.portal.clock.now())
    assert await bench.drain() == 0
    assert await _in_thread("reprocess", "resume") == 0
    assert await bench.drain() == 1
    rows = await bench.portal.rows("SELECT index_version, reprocess_error FROM kb_documents")
    assert [tuple(row) for row in rows] == [(2, None)]


async def test_reindex_recreate_collection_refuses_while_jobs_are_leased(
    bench: KbBench, command_line: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    client = await bench.portal.employee()
    await added(client, "документ.txt", "текст документа".encode())
    await bench.portal.execute("UPDATE kb_jobs SET locked_until = now() + interval '5 minutes'")
    qdrant = _local_worker(monkeypatch, command_line)
    index = QdrantVectorIndex(qdrant, command_line.kb)
    await index.ensure_collection()
    assert await _in_thread("reindex", "--recreate-collection") == 1
    assert "остановите воркер" in capsys.readouterr().err

    await bench.portal.execute("UPDATE kb_jobs SET locked_until = NULL")
    _local_worker(monkeypatch, command_line)
    assert await _in_thread("reindex", "--recreate-collection") == 0
    assert capsys.readouterr().out == "Документов возвращено в очередь: 1\n"


def test_reindex_flags_do_not_combine(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["reindex", "--only-errors", "--recreate-collection"])
    assert "not allowed with argument" in capsys.readouterr().err


async def test_unavailable_qdrant_is_reported_without_traceback(
    bench: KbBench, command_line: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    qdrant = command_line.kb.qdrant.model_copy(
        update={"url": "http://127.0.0.1:9", "timeout_seconds": 1}
    )
    broken = command_line.model_copy(
        update={"kb": command_line.kb.model_copy(update={"qdrant": qdrant})}
    )
    monkeypatch.setattr(cli, "load_settings", lambda env: broken)
    assert await _in_thread("reindex", "--recreate-collection") == 1
    assert capsys.readouterr().err == "Служба недоступна: VectorIndexUnavailableError\n"


async def test_eval_search_command_prints_report_and_leaves_no_collection(
    bench: KbBench, command_line: Settings, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """Команда целиком: настоящие клиенты модели, эмбеддингов и реранкера против заглушки."""
    async with live_server(create_stub(chunk_delay_ms=0)) as url:
        llm = command_line.llm.model_copy(update={"base_url": f"{url}/v1"})
        kb = command_line.kb
        kb = kb.model_copy(
            update={
                "rerank": kb.rerank.model_copy(update={"url": f"{url}/v1/rerank"}),
                # Флаг включён в конфигурации; стенд тестов выключает его для тестов очереди.
                "context": kb.context.model_copy(update={"enabled": True}),
            }
        )
        qdrant = _local_worker(monkeypatch, command_line.model_copy(update={"llm": llm, "kb": kb}))

        async def keep_open() -> None:
            """Команда закрывает клиент; тесту он ещё нужен, чтобы заглянуть в Qdrant."""

        monkeypatch.setattr(qdrant, "close", keep_open)
        assert await _in_thread("eval-search") == 0
        full = capsys.readouterr().out
        assert await _in_thread("eval-search", "--disable", "rerank", "--disable", "context") == 0
        partial = capsys.readouterr().out
        # Реранкер включён, но недоступен: числа были бы замером без него — команда отказывает.
        no_rerank = kb.model_copy(
            update={"rerank": kb.rerank.model_copy(update={"url": f"{url}/v1/absent"})}
        )
        broken = command_line.model_copy(update={"llm": llm, "kb": no_rerank})
        monkeypatch.setattr(_local_worker(monkeypatch, broken), "close", keep_open)
        assert await _in_thread("eval-search", "--disable", "context") == 1
        refused = capsys.readouterr()
    assert refused.out == "" and "не сработали ни на одном вопросе: rerank." in refused.err
    assert "все вопросы" in full and "hit@1" in full and "MRR" in full and "nDCG@8" in full
    assert "44 вопр." in full and "уточняющие" in full
    assert "Включено: rewrite, context, rerank" in full and "recall@40" in full
    # Замер «до и после» без правки конфигурации: улучшения выключаются на один прогон.
    assert "Включено: rewrite\n" in partial and "recall@8" in partial
    # Рабочая коллекция не создавалась, а коллекция оценки удалена.
    assert (await qdrant.get_collections()).collections == []
