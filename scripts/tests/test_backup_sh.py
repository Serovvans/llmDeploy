"""Логика backup.sh с подменённым ``docker``: состав копии, порядок действий, отказы.

Настоящая копия и восстановление проверяются на стенде (отдельный проект compose).
"""

import hashlib
import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "backup.sh"
PORTAL_SERVICES = ["vllm", "caddy", "bifrost", "portal-api", "portal-worker", "portal-db", "qdrant"]
ARCHIVES = ["portal-files.tgz", "qdrant-data.tgz", "bifrost-data.tgz"]
COPY_FILES = ["portal-db.dump", *ARCHIVES]
WRITERS = "portal-worker portal-api qdrant bifrost"
# Вызовы, после которых данные сервиса уже не прежние или сервисы остановлены.
INTRUSIVE = ("stop ", "ALTER DATABASE", "find . -mindepth")
STAGE_VOLUME = "sh -c set -e cd /vol rm -rf .restore-new"
SWITCH_VOLUME = "sh -c set -e cd /vol test -d .restore-new"
DISCARD_VOLUME = "rm -rf /vol/.restore-new /vol/.restore-new.ready"
RETRY = "повторить ту же команду восстановления"
# Записывает вызов одной строкой; отвечает на запросы к конфигурации compose; выдаёт
# данные для дампа и архивов; завершается с ошибкой, если вызов содержит строку из fail;
# шлёт SIGTERM скрипту, если вызов содержит строку из signal; сохраняет тело скрипта
# переключения тома, чтобы тест выполнил его на настоящем каталоге.
DOCKER_STUB = """#!/usr/bin/env bash
call="$(printf '%s' "$*" | tr -s '[:space:]' ' ')"
echo "$call" >>"$STUB_DIR/docker.log"
# Как настоящие exec -T и run -i: забирает весь доступный stdin.
if [[ "$call" == *"exec -T"* || "$call" == *"run --rm -i"* ]]; then cat >/dev/null; fi
if [[ -s "$STUB_DIR/fail" && "$call" == *"$(cat "$STUB_DIR/fail")"* ]]; then exit 1; fi
if [[ -s "$STUB_DIR/signal" && "$call" == *"$(cat "$STUB_DIR/signal")"* ]]; then
  kill -TERM "$PPID"
fi
if [[ "$call" == *"find . -mindepth"* ]]; then printf '%s' "${!#}" >"$STUB_DIR/switch_script"; fi
case "$call" in
  *"config --services") cat "$STUB_DIR/services" ;;
  *"config --images") printf 'postgres:16.15-bookworm\\ncaddy:2.11.4\\n' ;;
  *" config") printf 'name: %s\\nservices:\\n  x:\\n    name: y\\n' "$(cat "$STUB_DIR/project")" ;;
  *"FROM pg_database"*) cat "$STUB_DIR/trace_dbs" ;;
  *"ls -d .restore-new"*) cat "$STUB_DIR/trace_dirs" ;;
  *pg_dump* | *"tar czf"*) echo data ;;
esac
"""


def tgz(content: bytes = b"file content") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("./file.bin")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def write_checksums(copy: Path) -> None:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(copy.iterdir())
        if path.name != "SHA256SUMS"
    ]
    (copy / "SHA256SUMS").write_text("".join(lines))


class Sandbox:
    """Каталог с заглушкой ``docker`` и каталогом для копий."""

    def __init__(self, root: Path) -> None:
        self.stubs = root / "bin"
        self.target = root / "backups"
        self.stubs.mkdir()
        self.target.mkdir()
        stub = self.stubs / "docker"
        stub.write_text(DOCKER_STUB)
        stub.chmod(0o755)
        (self.stubs / "project").write_text("llm")
        self.set_traces()
        self.set_services(PORTAL_SERVICES)

    def set_services(self, services: list[str]) -> None:
        (self.stubs / "services").write_text("".join(f"{name}\n" for name in services))

    def set_traces(self, databases: str = "", directories: str = "") -> None:
        """Следы прерванного восстановления: лишние базы и каталоги в каждом томе."""
        (self.stubs / "trace_dbs").write_text(databases)
        (self.stubs / "trace_dirs").write_text(directories)

    def fail_on(self, fragment: str) -> None:
        (self.stubs / "fail").write_text(fragment)

    def signal_on(self, fragment: str) -> None:
        """На вызове с этой строкой заглушка шлёт SIGTERM скрипту и завершается успешно."""
        (self.stubs / "signal").write_text(fragment)

    def run(
        self, *args: str, stdin: str = "", closed_output: bool = False, no_reader: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Запускает скрипт; вывод можно сделать недоступным.

        ``closed_output`` — stdout и stderr закрыты, как при обрыве терминала;
        ``no_reader`` — это канал, читатель которого завершился (``make backup | tee``).
        """
        bash = shutil.which("bash") or "/bin/bash"
        env = {
            "PATH": f"{self.stubs}:/usr/bin:/bin",
            "STUB_DIR": str(self.stubs),
            "COMPOSE_FILE": "compose.yml",
        }
        command = [bash, str(SCRIPT), *args]
        if closed_output:
            command = [bash, "-c", 'exec "$0" "$@" >&- 2>&-', *command]
        if no_reader:
            read_end, write_end = os.pipe()
            os.close(read_end)
            try:
                return subprocess.run(
                    command,
                    input=stdin,
                    stdout=write_end,
                    stderr=write_end,
                    text=True,
                    env=env,
                    check=False,
                )
            finally:
                os.close(write_end)
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def restore(self, copy: Path, answer: str = "yes\n") -> subprocess.CompletedProcess[str]:
        return self.run("restore", str(copy), stdin=answer)

    def calls(self) -> list[str]:
        """Вызовы docker без общего префикса compose и без чтения конфигурации."""
        lines = (self.stubs / "docker.log").read_text().splitlines()
        calls = [line.removeprefix("compose -f compose.yml ") for line in lines]
        return [call for call in calls if not call.startswith("config")]

    def actions(self) -> list[str]:
        """Вызовы без проверок, ничего не меняющих (образ, тома, база, следы)."""
        checks = ("image inspect", "volume inspect", "exec -T portal-db pg_isready")
        probes = ("FROM pg_database", "ls -d .restore-new", "sh -c test -d /vol/.restore-new")
        return [
            call
            for call in self.calls()
            if not call.startswith(checks) and not any(probe in call for probe in probes)
        ]

    def assert_data_untouched(self) -> None:
        assert not [call for call in self.calls() if any(mark in call for mark in INTRUSIVE)]

    def make_copy(self, name: str = "llm-backup-20261005-120000") -> Path:
        """Целая копия: дамп, настоящие архивы и верные контрольные суммы."""
        copy = self.target / name
        copy.mkdir()
        (copy / "portal-db.dump").write_bytes(b"PGDMP" + b"x" * 1000)
        for archive in ARCHIVES:
            (copy / archive).write_bytes(tgz())
        write_checksums(copy)
        return copy


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def assert_refused(result: subprocess.CompletedProcess[str], *fragments: str) -> None:
    assert result.returncode == 1
    for fragment in fragments:
        assert fragment in result.stderr


# --- создание копии ------------------------------------------------------------


def test_create_dumps_database_and_archives_volumes_between_stop_and_start(
    sandbox: Sandbox,
) -> None:
    result = sandbox.run("create", str(sandbox.target))
    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert actions[0] == f"stop {WRITERS}"
    assert actions[1] == "exec -T portal-db pg_dump -U portal -d portal --format=custom"
    archived = [call for call in actions if "tar czf" in call]
    assert [call.split()[4:6] for call in archived] == [
        ["llm_portal-files:/vol:ro", "caddy:2.11.4"],
        ["llm_qdrant-data:/vol:ro", "caddy:2.11.4"],
        ["llm_bifrost-data:/vol:ro", "caddy:2.11.4"],
    ]
    assert all("--numeric-owner" in call for call in archived)
    assert all("--exclude ./.restore-new --exclude ./.restore-new.ready" in c for c in archived)
    assert actions[-1] == f"start {WRITERS}"

    (copy,) = sandbox.target.iterdir()
    assert copy.name.startswith("llm-backup-") and not copy.name.endswith(".partial")
    assert sorted(path.name for path in copy.iterdir()) == sorted([*COPY_FILES, "SHA256SUMS"])
    assert copy.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in copy.iterdir())
    assert "PORTAL_SECRET_KEY" in result.stdout


def test_create_writes_checksums_of_every_file(sandbox: Sandbox) -> None:
    assert sandbox.run("create", str(sandbox.target)).returncode == 0
    (copy,) = sandbox.target.iterdir()
    lines = (copy / "SHA256SUMS").read_text().splitlines()
    recorded = {line.split("  ", 1)[1]: line.split("  ", 1)[0] for line in lines}
    assert sorted(recorded) == sorted(COPY_FILES)
    for name, digest in recorded.items():
        assert hashlib.sha256((copy / name).read_bytes()).hexdigest() == digest


def test_created_copy_is_accepted_by_restore_checks(sandbox: Sandbox) -> None:
    """Суммы, записанные create, проходят сверку restore (архивы заглушки — не tar)."""
    assert sandbox.run("create", str(sandbox.target)).returncode == 0
    (copy,) = sandbox.target.iterdir()
    assert_refused(sandbox.restore(copy), "не читается как архив")
    assert "контрольных сумм" in sandbox.restore(copy).stdout


def test_create_without_portal_copies_only_bifrost(sandbox: Sandbox) -> None:
    sandbox.set_services(["vllm", "caddy", "bifrost"])
    assert sandbox.run("create", str(sandbox.target)).returncode == 0
    actions = sandbox.actions()
    assert actions[0] == "stop bifrost"
    assert not any("pg_dump" in call for call in actions)
    (copy,) = sandbox.target.iterdir()
    assert sorted(path.name for path in copy.iterdir()) == ["SHA256SUMS", "bifrost-data.tgz"]


def test_create_refuses_when_nothing_to_copy(sandbox: Sandbox) -> None:
    sandbox.set_services(["vllm"])
    assert_refused(sandbox.run("create", str(sandbox.target)), "копировать нечего")
    assert list(sandbox.target.iterdir()) == []


def test_failed_create_restarts_services_and_leaves_partial_copy(sandbox: Sandbox) -> None:
    sandbox.fail_on("pg_dump")
    assert_refused(sandbox.run("create", str(sandbox.target)), "pg_dump не выполнен")
    assert sandbox.calls()[-1] == f"start {WRITERS}"
    (copy,) = sandbox.target.iterdir()
    assert copy.name.endswith(".partial")
    assert not (copy / "SHA256SUMS").exists()


@pytest.mark.parametrize("no_reader", [False, True])
def test_failed_create_restarts_services_when_terminal_is_gone(
    sandbox: Sandbox, no_reader: bool
) -> None:
    """Сообщения некуда писать: это не должно оборвать возврат сервисов в работу."""
    sandbox.fail_on("pg_dump")
    result = sandbox.run(
        "create", str(sandbox.target), closed_output=not no_reader, no_reader=no_reader
    )
    assert result.returncode == 1
    actions = sandbox.actions()
    assert actions[0] == f"stop {WRITERS}"
    assert actions[-1] == f"start {WRITERS}"


def test_signal_during_final_start_does_not_leave_copy_partial(sandbox: Sandbox) -> None:
    """Архивы уже сняты: сигнал на запуске сервисов не обрывает скрипт молча."""
    sandbox.signal_on(f"start {WRITERS}")
    result = sandbox.run("create", str(sandbox.target))
    assert result.returncode == 0, result.stderr
    assert "Копия готова" in result.stdout
    assert sandbox.calls()[-1] == f"start {WRITERS}"
    (copy,) = sandbox.target.iterdir()
    assert not copy.name.endswith(".partial")
    assert (copy / "SHA256SUMS").exists()


@pytest.mark.parametrize(
    ("failing", "message"),
    [
        ("volume inspect llm_qdrant-data", "нет тома llm_qdrant-data"),
        ("image inspect caddy:2.11.4", "нет образа caddy:2.11.4"),
        ("pg_isready", "portal-db не отвечает"),
    ],
)
@pytest.mark.parametrize("command", ["create", "restore"])
def test_prerequisites_are_checked_before_services_stop(
    sandbox: Sandbox, command: str, failing: str, message: str
) -> None:
    sandbox.fail_on(failing)
    path = sandbox.make_copy() if command == "restore" else sandbox.target
    assert_refused(sandbox.run(command, str(path), stdin="yes\n"), message)
    assert sandbox.actions() == []


def test_volumes_belong_to_project_named_in_compose_file(sandbox: Sandbox) -> None:
    (sandbox.stubs / "project").write_text("portal-dev")
    assert sandbox.run("create", str(sandbox.target)).returncode == 0
    mounts = [call.split()[4] for call in sandbox.calls() if "tar czf" in call]
    assert mounts == [
        "portal-dev_portal-files:/vol:ro",
        "portal-dev_qdrant-data:/vol:ro",
        "portal-dev_bifrost-data:/vol:ro",
    ]


# --- восстановление: порядок ---------------------------------------------------


def test_restore_stages_everything_before_switching(sandbox: Sandbox) -> None:
    result = sandbox.restore(sandbox.make_copy())
    assert result.returncode == 0, result.stderr
    actions = sandbox.actions()
    assert actions[:4] == [
        "exec -T portal-db pg_restore --list",
        "exec -T portal-db dropdb -U portal --if-exists --force portal_restore",
        "exec -T portal-db createdb -U portal portal_restore",
        "exec -T portal-db pg_restore -U portal -d portal_restore --exit-on-error",
    ]
    assert actions[4] == f"stop {WRITERS}"
    staged = [index for index, call in enumerate(actions) if "tar xzf" in call]
    renamed = next(index for index, call in enumerate(actions) if "ALTER DATABASE" in call)
    switched = [index for index, call in enumerate(actions) if "find . -mindepth" in call]
    assert len(staged) == len(switched) == 3
    assert max(staged) < renamed < min(switched)
    assert all(STAGE_VOLUME in actions[index] for index in staged)
    assert all("--numeric-owner" in actions[index] for index in staged)
    # Отметка готовности ставится после распаковки, а прежнее удаляется после её проверки.
    assert all(actions[index].rstrip().endswith("touch .restore-new.ready") for index in staged)
    for index in switched:
        script = actions[index]
        assert script.index("test -f .restore-new.ready") < script.index("find . -mindepth")
    terminated = next(i for i, call in enumerate(actions) if "pg_terminate_backend" in call)
    assert terminated == renamed - 1
    assert "portal RENAME TO portal_previous" in actions[renamed]
    assert "portal_restore RENAME TO portal" in actions[renamed]
    assert "--single-transaction" in actions[renamed]
    assert actions[-2:] == [
        "exec -T portal-db dropdb -U portal --if-exists --force portal_previous",
        f"start {WRITERS}",
    ]
    # Рабочая база не удаляется никогда: только переименование.
    assert not any(call.endswith("--force portal") for call in actions)


@pytest.mark.parametrize("answer", ["", "no\n", "y\n"])
def test_restore_touches_nothing_without_yes(sandbox: Sandbox, answer: str) -> None:
    assert_refused(sandbox.restore(sandbox.make_copy(), answer), "отменено")
    assert sandbox.actions() == ["exec -T portal-db pg_restore --list"]


# --- восстановление: негодная копия отвергается до любых изменений -------------


def test_restore_refuses_partial_copy(sandbox: Sandbox) -> None:
    copy = sandbox.make_copy("llm-backup-20261005-120000.partial")
    assert_refused(sandbox.restore(copy), "незавершённая копия")
    assert sandbox.actions() == []


def test_restore_refuses_copy_without_checksums(sandbox: Sandbox) -> None:
    copy = sandbox.make_copy()
    (copy / "SHA256SUMS").unlink()
    assert_refused(sandbox.restore(copy), "нет SHA256SUMS")
    assert sandbox.actions() == []


@pytest.mark.parametrize("missing", COPY_FILES)
def test_restore_refuses_incomplete_copy(sandbox: Sandbox, missing: str) -> None:
    copy = sandbox.make_copy()
    (copy / missing).unlink()
    assert_refused(sandbox.restore(copy), missing)
    assert sandbox.actions() == []


def test_restore_refuses_file_missing_from_checksums(sandbox: Sandbox) -> None:
    copy = sandbox.make_copy()
    sums = (copy / "SHA256SUMS").read_text().splitlines()
    kept = [line for line in sums if "qdrant" not in line]
    (copy / "SHA256SUMS").write_text("\n".join(kept) + "\n")
    assert_refused(sandbox.restore(copy), "нет записи о qdrant-data.tgz")
    assert sandbox.actions() == []


def test_restore_refuses_truncated_dump(sandbox: Sandbox) -> None:
    copy = sandbox.make_copy()
    dump = copy / "portal-db.dump"
    dump.write_bytes(dump.read_bytes()[:100])
    assert_refused(sandbox.restore(copy), "контрольные суммы копии не совпадают")
    assert sandbox.actions() == []


def test_restore_refuses_archive_changed_in_transit(sandbox: Sandbox) -> None:
    copy = sandbox.make_copy()
    (copy / "portal-files.tgz").write_bytes(tgz(b"other content"))
    assert_refused(sandbox.restore(copy), "контрольные суммы копии не совпадают")
    assert sandbox.actions() == []


def test_restore_refuses_unreadable_archive_even_with_matching_checksum(sandbox: Sandbox) -> None:
    copy = sandbox.make_copy()
    (copy / "portal-files.tgz").write_bytes(b"garbage, not a gzip archive")
    write_checksums(copy)
    assert_refused(sandbox.restore(copy), "portal-files.tgz не читается как архив")
    sandbox.assert_data_untouched()


def test_restore_refuses_unreadable_dump_even_with_matching_checksum(sandbox: Sandbox) -> None:
    sandbox.fail_on("pg_restore --list")
    assert_refused(sandbox.restore(sandbox.make_copy()), "не читается как дамп")
    sandbox.assert_data_untouched()


# --- восстановление: сбой на каждом шаге ---------------------------------------


def test_dump_that_fails_to_load_leaves_everything_running(sandbox: Sandbox) -> None:
    sandbox.fail_on("pg_restore -U portal -d portal_restore")
    assert_refused(sandbox.restore(sandbox.make_copy()), "НЕ изменены", "временную базу")
    sandbox.assert_data_untouched()
    assert sandbox.calls()[-1].endswith("dropdb -U portal --if-exists --force portal_restore")


def test_services_that_fail_to_stop_are_restarted_and_staging_is_discarded(
    sandbox: Sandbox,
) -> None:
    sandbox.fail_on(f"stop {WRITERS}")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(result, "сервисы не остановлены", "НЕ изменены")
    calls = sandbox.calls()
    assert not [call for call in calls if "ALTER DATABASE" in call or "tar xzf" in call]
    assert calls[-2].endswith("dropdb -U portal --if-exists --force portal_restore")
    assert calls[-1] == f"start {WRITERS}"


def test_archive_that_fails_to_unpack_leaves_old_data_and_restarts_services(
    sandbox: Sandbox,
) -> None:
    sandbox.fail_on(f"llm_qdrant-data:/vol caddy:2.11.4 {STAGE_VOLUME}")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(result, "НЕ изменены", "qdrant-data.tgz не распаковался")
    calls = sandbox.calls()
    assert not [call for call in calls if "ALTER DATABASE" in call or "find . -mindepth" in call]
    assert sum(DISCARD_VOLUME in call for call in calls) == 3
    assert calls[-1] == f"start {WRITERS}"


def test_staging_lost_by_volume_is_noticed_before_anything_is_switched(sandbox: Sandbox) -> None:
    """Том, не сохраняющий данные между контейнерами: прежнее содержимое не удаляется."""
    sandbox.fail_on("llm_qdrant-data:/vol:ro caddy:2.11.4 sh -c test -d /vol/.restore-new")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(result, "не сохранилось в томе qdrant-data", "НЕ изменены")
    calls = sandbox.calls()
    assert not [call for call in calls if "ALTER DATABASE" in call or "find . -mindepth" in call]
    assert calls[-1] == f"start {WRITERS}"


def test_failed_database_switch_changes_nothing_and_restarts_services(sandbox: Sandbox) -> None:
    sandbox.fail_on("ALTER DATABASE")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(result, "база портала не переключена", "НЕ изменены")
    calls = sandbox.calls()
    assert not any("find . -mindepth" in call for call in calls)
    assert calls[-1] == f"start {WRITERS}"


def test_failed_volume_switch_reports_what_is_already_new(sandbox: Sandbox) -> None:
    sandbox.fail_on(f"llm_qdrant-data:/vol caddy:2.11.4 {SWITCH_VOLUME}")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(
        result,
        "том qdrant-data переключён не полностью",
        "переключено на данные копии: база портала том portal-files.",
        "смешанными",
        RETRY,
    )
    calls = sandbox.calls()
    assert not any(call.startswith("start") for call in calls)
    # Следы остаются: по ним повтор и create узнают о незавершённом восстановлении.
    assert not any(DISCARD_VOLUME in call for call in calls)
    assert not any(call.endswith("--force portal_previous") for call in calls[-3:])


def test_failed_switch_of_single_volume_is_not_reported_as_unchanged(sandbox: Sandbox) -> None:
    """Без базы (только Bifrost): сбой внутри переключения тома — уже не «прежние данные»."""
    sandbox.set_services(["vllm", "caddy", "bifrost"])
    copy = sandbox.make_copy()
    sandbox.fail_on(SWITCH_VOLUME)
    result = sandbox.restore(copy)
    assert_refused(result, "смешанными", RETRY)
    assert "НЕ изменены" not in result.stderr
    assert not any(call.startswith("start") for call in sandbox.calls())


@pytest.mark.parametrize("present", [".restore-new", ".restore-new.ready"])
def test_volume_switch_keeps_old_content_without_complete_staging(
    sandbox: Sandbox, tmp_path: Path, present: str
) -> None:
    """Тело switch_volume выполняется в sh: без каталога или без отметки прежнее цело."""
    assert sandbox.restore(sandbox.make_copy()).returncode == 0
    script = (sandbox.stubs / "switch_script").read_text()
    assert "cd /vol" in script
    volume = tmp_path / "volume"
    volume.mkdir()
    (volume / "data.bin").write_text("old")
    if present == ".restore-new":
        (volume / present).mkdir()
    else:
        (volume / present).touch()
    result = subprocess.run(
        ["sh", "-c", script.replace("cd /vol", "cd .")], cwd=volume, check=False
    )
    assert result.returncode != 0
    assert (volume / "data.bin").read_text() == "old"


# --- восстановление: прерывание сигналом ---------------------------------------


def test_signal_while_unpacking_discards_staging_and_restarts_services(sandbox: Sandbox) -> None:
    sandbox.signal_on(f"llm_qdrant-data:/vol caddy:2.11.4 {STAGE_VOLUME}")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(result, "восстановление прервано", "НЕ изменены")
    calls = sandbox.calls()
    assert not [call for call in calls if "ALTER DATABASE" in call or "find . -mindepth" in call]
    assert sum("tar xzf" in call for call in calls) == 2
    assert sum(DISCARD_VOLUME in call for call in calls) == 3
    assert any(
        call.endswith("dropdb -U portal --if-exists --force portal_restore") for call in calls[-2:]
    )
    assert calls[-1] == f"start {WRITERS}"


def test_signal_while_stopping_services_restarts_them(sandbox: Sandbox) -> None:
    sandbox.signal_on(f"stop {WRITERS}")
    assert_refused(sandbox.restore(sandbox.make_copy()), "восстановление прервано", "НЕ изменены")
    assert sandbox.calls()[-1] == f"start {WRITERS}"


def test_signal_while_switching_database_is_not_reported_as_unchanged(sandbox: Sandbox) -> None:
    """Сигнал не говорит, успело ли переименование выполниться: состояние смешанное."""
    sandbox.signal_on("ALTER DATABASE")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(result, "восстановление прервано", "смешанными", RETRY)
    assert "НЕ изменены" not in result.stderr
    calls = sandbox.calls()
    assert not any(call.startswith("start") for call in calls)
    assert not any(DISCARD_VOLUME in call for call in calls)


def test_signal_during_final_start_does_not_cut_restore_short(sandbox: Sandbox) -> None:
    """Данные уже новые: сигнал на запуске сервисов не обрывает скрипт молча."""
    sandbox.signal_on(f"start {WRITERS}")
    result = sandbox.restore(sandbox.make_copy())
    assert result.returncode == 0, result.stderr
    assert "Восстановление завершено" in result.stdout
    assert sandbox.calls()[-1] == f"start {WRITERS}"


# --- следы прерванного восстановления ------------------------------------------


@pytest.mark.parametrize(
    ("databases", "directories", "trace"),
    [
        ("portal_previous\n", "", "база portal_previous"),
        ("portal_restore\n", "", "база portal_restore"),
        ("", ".restore-new\n", "том llm_portal-files: .restore-new"),
        ("", ".restore-new.ready\n", "том llm_qdrant-data: .restore-new.ready"),
    ],
)
def test_create_refuses_after_interrupted_restore(
    sandbox: Sandbox, databases: str, directories: str, trace: str
) -> None:
    sandbox.set_traces(databases, directories)
    result = sandbox.run("create", str(sandbox.target))
    assert_refused(result, "восстановление не завершено", trace, RETRY)
    assert sandbox.actions() == []
    assert list(sandbox.target.iterdir()) == []


def test_retry_after_interrupted_restore_completes(sandbox: Sandbox) -> None:
    sandbox.set_traces("portal_previous\n", ".restore-new\n")
    result = sandbox.restore(sandbox.make_copy())
    assert result.returncode == 0, result.stderr
    assert "следы прерванного восстановления" in result.stdout
    assert sandbox.calls()[-1] == f"start {WRITERS}"


def test_failed_retry_does_not_claim_old_data_or_running_services(sandbox: Sandbox) -> None:
    """Повтор после прерванного переключения: дамп не загрузился — состояние смешанное."""
    sandbox.set_traces("portal_previous\n", "")
    sandbox.fail_on("pg_restore -U portal -d portal_restore")
    result = sandbox.restore(sandbox.make_copy())
    assert_refused(
        result,
        "дамп не загрузился",
        "уже было прервано (база portal_previous)",
        "смешанными",
        "не запущены",
        RETRY,
    )
    assert "НЕ изменены" not in result.stderr
    calls = sandbox.calls()
    assert not any(call.startswith("start") for call in calls)
    assert not any(DISCARD_VOLUME in call for call in calls)
    assert "pg_restore -U portal -d portal_restore" in calls[-1]


@pytest.mark.parametrize("args", [[], ["create"], ["restore"], ["purge", "x"]])
def test_usage_errors(sandbox: Sandbox, args: list[str]) -> None:
    assert_refused(sandbox.run(*args), "ОШИБКА")
