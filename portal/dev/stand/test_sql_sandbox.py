"""Сервис sql-sandbox на поднятом стенде: сеть, образ, параметры сервера, журнал, перезапуск.

Требования — docs/portal-api.md §7.4 (таблица сервиса и проверки изоляции, закреплённые за
инфраструктурой) и docs/portal-design.md §10, п. 18. Поведение порта портала (сроки,
чтение результата, изоляция проверок друг от друга) проверяют тесты бэкенда.

Запуск: `uv run pytest stand` при поднятом стенде (make dev-up). Проверки роняют сервер
песочницы — идущие в это время проверки SQL на стенде получат «недоступно».
"""

import json
import subprocess
from collections.abc import Iterator

import pytest

from stand.support import (
    COMPOSE_FILE,
    PROJECT,
    compose,
    container_id,
    container_log,
    docker,
    ok,
    wait_until,
)

RUN_ROLE = "sandbox_run_1"
# Пароль psql берёт из окружения контейнера: проверкам секреты стенда читать незачем.
ROLE_PSQL = (
    'PGPASSWORD="$SQL_SANDBOX_PASSWORD" psql -X -At -v ON_ERROR_STOP=1'
    f" -h 127.0.0.1 -U {RUN_ROLE} -d sandbox"
)
# Суперпользователь входит только через сокет (peer) — так проверки смотрят на сервер извне
# роли проверки.
SUPER_PSQL = "psql -X -At -v ON_ERROR_STOP=1 -U postgres -d sandbox"
# Оператор снимает предел времени сам, как это сделал бы враждебный запрос. Ряд порождается
# в списке SELECT: из FROM он ушёл бы во временный файл и упёрся в temp_file_limit раньше,
# чем в tmpfs.
FILL_PREFIX = "SET statement_timeout = 0; BEGIN; CREATE SCHEMA fill;"
ROWS = (
    "SELECT repeat(md5(g::text), 30) AS marker_fill FROM (SELECT generate_series(1, {}) AS g) AS s"
)
# Таблица упирается в tmpfs сама: сервер отвечает ошибкой оператора.
FILL_DATA = f"{FILL_PREFIX} CREATE TABLE fill.t AS {ROWS.format(100_000_000)};"
# Место кончается у WAL, а это PANIC — abort() сервера со строкой STATEMENT в его выводе.
# Нежурналируемая таблица занимает tmpfs, не трогая WAL; дальше удаление с откатом пишет
# WAL, не требуя места под данные. Вставка в журналируемую таблицу до PANIC доходила не
# всегда: когда место раньше кончалось у данных, неудачные вставки в WAL уже ничего не
# писали, и цикл крутился без конца.
FILL_WAL = (
    f"{FILL_PREFIX} CREATE TABLE fill.w AS {ROWS.format(5000)};"
    " CREATE UNLOGGED TABLE fill.t (marker_fill text);"
    " DO $$ BEGIN"
    f" BEGIN LOOP INSERT INTO fill.t {ROWS.format(1000)}; END LOOP;"
    " EXCEPTION WHEN disk_full THEN NULL; END;"
    " LOOP BEGIN DELETE FROM fill.w; RAISE EXCEPTION 'again';"
    " EXCEPTION WHEN raise_exception THEN NULL; END; END LOOP;"
    " END $$;"
)
# Обычно PANIC наступает за секунду; срок — с запасом на загруженную машину.
FILL_WAL_TIMEOUT = 60
TERMINATE_RUN_ROLE = (
    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = '{RUN_ROLE}'"
)


def sandbox_sh(
    script: str, *, stdin: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    return compose("exec", "-T", "sql-sandbox", "bash", "-c", script, stdin=stdin, timeout=timeout)


def as_role(sql: str, *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return sandbox_sh(ROLE_PSQL, stdin=sql, timeout=timeout)


def as_superuser(sql: str) -> str:
    return ok(sandbox_sh(SUPER_PSQL, stdin=sql))


def restart_count(container: str) -> int:
    return int(ok(docker("inspect", "-f", "{{.RestartCount}}", container)))


def wait_ready() -> None:
    wait_until(lambda: as_role("SELECT 1").returncode == 0, "песочница принимает роль проверки")


def assert_next_check_passes() -> None:
    """Следующая проверка проходит: место в tmpfs есть, следов прежней в базе нет."""
    wait_ready()
    assert (
        as_superuser("SELECT count(*) FROM pg_namespace WHERE nspname IN ('fill', 'killed')") == "0"
    )
    result = as_role(
        "BEGIN; CREATE SCHEMA next_check;"
        " CREATE TABLE next_check.t AS SELECT g, md5(g::text) FROM generate_series(1, 200000) AS g;"
        " SELECT count(*) FROM next_check.t; ROLLBACK;"
    )
    assert result.returncode == 0, result.stderr
    assert "200000" in result.stdout


def denied(sql: str, expected: str) -> None:
    result = as_role(sql)
    assert result.returncode != 0, f"сервер исполнил: {sql}"
    assert expected in result.stderr, result.stderr


@pytest.fixture(scope="module")
def sandbox() -> Iterator[str]:
    container = container_id("sql-sandbox")
    wait_ready()
    yield container
    wait_ready()


def test_only_sandbox_network_and_no_host_ports(sandbox: str) -> None:
    settings = json.loads(ok(docker("inspect", "-f", "{{json .NetworkSettings}}", sandbox)))
    assert list(settings["Networks"]) == [f"{PROJECT}_sandbox"]
    assert not any(settings["Ports"].values())


def test_no_connection_to_portal_db_bifrost_or_outside(sandbox: str) -> None:
    def address(service: str) -> str:
        template = "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}"
        return ok(docker("inspect", "-f", template, container_id(service))).split()[0]

    def connects(host: str, port: int) -> bool:
        probe = f"timeout 5 bash -c 'exec 3<>/dev/tcp/{host}/{port}'"
        return sandbox_sh(probe).returncode == 0

    # Сама проба работает, а portal-api до песочницы достаёт — иначе отказы ниже ничего
    # не значили бы.
    assert connects("127.0.0.1", 5432)
    reach = "import socket; socket.create_connection(('sql-sandbox', 5432), 5)"
    assert compose("exec", "-T", "portal-api", "python", "-c", reach).returncode == 0

    for name in ("portal-db", "bifrost", "example.com"):
        assert sandbox_sh(f"getent hosts {name}").returncode != 0, name
    # По адресу, а не только по имени: имя чужой сети не разрешается и при открытом пути.
    assert not connects(address("portal-db"), 5432)
    assert not connects(address("bifrost"), 8080)
    assert not connects("1.1.1.1", 443)


def test_image_has_only_plpgsql_and_postgis(sandbox: str) -> None:
    available = as_superuser("SELECT name FROM pg_available_extensions ORDER BY name")
    assert available.split() == ["plpgsql", "postgis"]
    # hstore, pg_trgm и pgcrypto помечены trusted: будь их файлы в образе, роль с правом
    # CREATE на базу поставила бы их сама.
    for extension in ("hstore", "pg_trgm", "pgcrypto", "postgis_raster", "dblink", "postgres_fdw"):
        denied(f"CREATE EXTENSION {extension}", "is not available")
    geometry = as_role("SELECT public.ST_AsText(public.ST_MakePoint(1, 2))")
    assert geometry.stdout.strip() == "POINT(1 2)", geometry.stderr


def test_role_reads_no_files_and_runs_no_programs(sandbox: str) -> None:
    for sql in (
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "BEGIN; CREATE SCHEMA c; CREATE TABLE c.t (line text); COPY c.t FROM '/etc/passwd'",
        "BEGIN; CREATE SCHEMA c; CREATE TABLE c.t (line text); COPY c.t FROM PROGRAM 'id'",
        "COPY (SELECT 1) TO '/var/lib/postgresql/data/out'",
        "SELECT pg_logical_emit_message(false, 'prefix', repeat('x', 1000))",
    ):
        denied(sql, "permission denied")


def test_server_starts_no_parallel_workers_or_prepared_transactions(sandbox: str) -> None:
    plan = as_role(
        "BEGIN; CREATE SCHEMA par;"
        " CREATE TABLE par.t AS SELECT g FROM generate_series(1, 200000) AS g; ANALYZE par.t;"
        " SET max_parallel_workers = 8; SET max_parallel_workers_per_gather = 4;"
        " SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0;"
        " SET min_parallel_table_scan_size = 0;"
        " EXPLAIN (ANALYZE, COSTS OFF, TIMING OFF) SELECT count(*) FROM par.t; ROLLBACK;"
    )
    assert plan.returncode == 0, plan.stderr
    # Планировщик исполнителей запросил — значит, проверяется именно отказ сервера их дать.
    assert "Workers Planned:" in plan.stdout
    assert "Workers Launched: 0" in plan.stdout
    denied(
        "BEGIN; CREATE SCHEMA prep; PREPARE TRANSACTION 'outlives_session'",
        "prepared transactions are disabled",
    )


def test_no_core_dumps(sandbox: str) -> None:
    assert ok(sandbox_sh("ulimit -c")) == "0"
    assert ok(compose("exec", "-T", "portal-worker", "sh", "-c", "ulimit -c")) == "0"


def test_log_is_empty_after_error_timeout_and_failed_login(sandbox: str) -> None:
    denied("SELECT 1 / 0 AS marker_error", "division by zero")
    denied(
        "SET statement_timeout = '200ms'; SELECT pg_sleep(5), 'marker_timeout'", "statement timeout"
    )
    wrong_password = sandbox_sh(
        f"PGPASSWORD=wrong psql -h 127.0.0.1 -U {RUN_ROLE} -d sandbox -c 'SELECT 1'"
    )
    assert wrong_password.returncode != 0
    assert container_log(sandbox) == ""


def test_filling_tmpfs_with_data_fails_statement_and_leaves_no_log(sandbox: str) -> None:
    filled = as_role(FILL_DATA, timeout=300)
    assert "No space left on device" in filled.stderr, filled.stderr
    assert_next_check_passes()
    assert container_log(sandbox) == ""


def test_filling_tmpfs_with_wal_restarts_container_and_leaves_no_log(sandbox: str) -> None:
    restarts = restart_count(sandbox)
    try:
        filled = as_role(FILL_WAL, timeout=FILL_WAL_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Клиент убит, а сеанс в контейнере жив и держит tmpfs заполненным для следующих
        # проверок.
        as_superuser(TERMINATE_RUN_ROLE)
        pytest.fail(f"за {FILL_WAL_TIMEOUT} с до PANIC не дошло: место у WAL не кончилось")
    assert filled.returncode != 0, filled.stdout
    # Перезапуска нет — значит, до PANIC дело не дошло и проверка ничего не проверила.
    wait_until(lambda: restart_count(sandbox) > restarts, "контейнер песочницы перезапущен")
    assert_next_check_passes()
    assert container_log(sandbox) == ""


def test_killed_session_restarts_container_with_empty_database(sandbox: str) -> None:
    # Зафиксированная таблица: при восстановлении сервера на месте она пережила бы сбой.
    as_superuser("CREATE TABLE public.survivor (id int)")
    restarts = restart_count(sandbox)
    compose_exec = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE), "exec", "-T"]
    session = subprocess.Popen(
        [*compose_exec, "sql-sandbox", "bash", "-c", ROLE_PSQL],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        assert session.stdin is not None
        session.stdin.write(
            "SET statement_timeout = 0; BEGIN; CREATE SCHEMA killed;"
            " CREATE TABLE killed.t AS SELECT g, md5(g::text) AS marker_killed"
            " FROM generate_series(1, 300000) AS g;"
            " SELECT pg_sleep(300);\n"
        )
        session.stdin.close()
        find_pid = (
            f"SELECT pid FROM pg_stat_activity WHERE usename = '{RUN_ROLE}'"
            " AND wait_event = 'PgSleep'"
        )
        wait_until(lambda: as_superuser(find_pid) != "", "сеанс проверки дошёл до ожидания")
        ok(sandbox_sh(f"kill -9 {as_superuser(find_pid)}"))
        wait_until(lambda: restart_count(sandbox) > restarts, "контейнер песочницы перезапущен")
    finally:
        session.kill()
        session.wait()
    wait_ready()
    assert as_superuser("SELECT to_regclass('public.survivor') IS NULL") == "t"
    assert_next_check_passes()
    assert container_log(sandbox) == ""
