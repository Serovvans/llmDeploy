"""Логика release.sh, не требующая ВМ: место копии, репетиция миграций, откат.

Скрипт подключается через ``source`` во временном дереве из ``test_install_sh`` — это
релизный клон. Рабочий клон — отдельное дерево прежнего коммита: в нём нет ``release.sh``,
а ``install.sh`` не содержит функций релиза. Заглушки ``backup.sh`` и ``stack_up.sh``
рабочего клона, ``systemctl`` и ``df`` пишут вызовы в общий с ``docker`` журнал.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
from test_install_sh import COMMIT, GATEWAY_ENV, REPO, Sandbox, assert_refused

RECORDING_STUB = '#!/usr/bin/env bash\necho "{name} $*" >>"$STUB_DIR/docker.log"\n'
# Устройство каталога копии — файл device; у корня оно всегда /dev/root.
DF_STUB = """#!/usr/bin/env bash
device=/dev/root
if [[ "${!#}" != "/" && -e "$STUB_DIR/device" ]]; then device="$(cat "$STUB_DIR/device")"; fi
printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n%s 1 1 1 1%% /x\\n' "$device"
"""
# install.sh прежнего коммита: функций и констант релиза в нём нет, вызывать его нельзя.
PREVIOUS_INSTALL = '#!/usr/bin/env bash\necho "вызван install.sh прежнего коммита" >&2\nexit 97\n'
PREVIOUS_COMMIT = "feedfacecafe"
PORTAL_SERVICES = ("portal-api", "portal-worker", "portal-web")
PREVIOUS_ENV = {
    **GATEWAY_ENV,
    "COMPOSE_PROFILES": "gateway,embeddings,portal",
    "GPU_MEM_UTIL": "0.85",
}
FAILED_RELEASE_ENV = {
    **PREVIOUS_ENV,
    "COMPOSE_PROFILES": "gateway,embeddings,rerank,portal",
    "GPU_MEM_UTIL": "0.81",
    "PORTAL_IMAGE_TAG": "ffffffffffff",
}


class ReleaseSandbox(Sandbox):
    """Релизный клон на коммите ``COMMIT`` и рабочий клон ``live`` на ``PREVIOUS_COMMIT``."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        shutil.copy(REPO / "scripts" / "release.sh", root / "scripts")
        self.live = root / "live"
        self.live_env = self.live / "deploy" / ".env"
        (self.live / "scripts").mkdir(parents=True)
        (self.live / "deploy").mkdir()
        (self.live / "deploy" / "docker-compose.yml").touch()
        (self.live / ".commit").write_text(PREVIOUS_COMMIT + "\n")
        (self.live / "scripts" / "install.sh").write_text(PREVIOUS_INSTALL)
        for name in ("backup.sh", "stack_up.sh"):
            (self.live / "scripts" / name).write_text(RECORDING_STUB.format(name=name))
        for name, body in (("systemctl", RECORDING_STUB.format(name="systemctl")), ("df", DF_STUB)):
            stub = self.stubs / name
            stub.write_text(body)
            stub.chmod(0o755)
        (self.stubs / "services").write_text("vllm\n" + "".join(f"{s}\n" for s in PORTAL_SERVICES))

    def release(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        script = f'source "{self.root}/scripts/release.sh"\nmain "$@"\n'
        path = f"{self.stubs}:/usr/bin:/bin:/usr/sbin:/sbin"
        bash = shutil.which("bash") or "/bin/bash"
        return subprocess.run(
            [bash, "-c", script, "release.sh", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env={"PATH": path, "STUB_DIR": str(self.stubs)},
            check=False,
        )

    def rollback(self, *args: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return self.release("rollback", str(self.live), *args, stdin=stdin)

    def write_live_env(self, values: dict[str, str]) -> None:
        """Записать ``.env`` рабочего клона; в релизном клоне своего ``.env`` нет."""
        self.write_env(values)
        self.env_file.replace(self.live_env)

    def save_previous_env(self) -> str:
        """Положить ``.env`` прежнего релиза туда, куда его сохраняет установка."""
        self.write_live_env(PREVIOUS_ENV)
        saved = self.live / "deploy" / "rollback" / ".env"
        saved.parent.mkdir()
        lines = self.live_env.read_text().splitlines()
        saved.write_text("\n".join(line for line in lines if "PORTAL_IMAGE_TAG" not in line) + "\n")
        self.write_live_env(FAILED_RELEASE_ENV)
        return saved.read_text()


@pytest.fixture
def sandbox(tmp_path: Path) -> ReleaseSandbox:
    return ReleaseSandbox(tmp_path)


# --- резервная копия перед релизом ---------------------------------------------


def test_backup_is_refused_on_the_root_disk(sandbox: ReleaseSandbox, tmp_path: Path) -> None:
    result = sandbox.release("backup", str(sandbox.live), str(tmp_path))
    assert_refused(result, "лежит на диске /")
    assert sandbox.docker_calls() == []


def test_backup_goes_to_another_disk(sandbox: ReleaseSandbox, tmp_path: Path) -> None:
    (sandbox.stubs / "device").write_text("/dev/sdb1\n")
    result = sandbox.release("backup", str(sandbox.live), str(tmp_path))
    assert result.returncode == 0, result.stderr
    # Заглушка backup.sh есть только в рабочем клоне: копию снимает сценарий прежнего коммита.
    assert sandbox.docker_calls() == [f"backup.sh create {tmp_path}"]


@pytest.mark.parametrize("command", ["backup", "rollback"])
def test_directory_that_is_not_a_live_clone_is_refused(
    sandbox: ReleaseSandbox, tmp_path: Path, command: str
) -> None:
    (sandbox.stubs / "device").write_text("/dev/sdb1\n")
    # Релизный клон вместо рабочего: backup.sh и compose-файла в песочнице у него нет.
    assert_refused(sandbox.release(command, str(tmp_path), str(tmp_path)), "не похож на рабочий")
    assert sandbox.docker_calls() == []


# --- откат ----------------------------------------------------------------------


def previous_images() -> list[str]:
    return [f"llm-{service}:{PREVIOUS_COMMIT}" for service in PORTAL_SERVICES]


def test_rollback_restores_env_and_starts_previous_images(sandbox: ReleaseSandbox) -> None:
    """Откат из релизного клона: в рабочем — прежний коммит без сценариев релиза."""
    saved = sandbox.save_previous_env()
    sandbox.set_images(*previous_images())
    result = sandbox.rollback()
    assert result.returncode == 0, result.stderr

    restored = sandbox.live_env.read_text()
    assert restored == saved + f"PORTAL_IMAGE_TAG={PREVIOUS_COMMIT}\n"
    assert sandbox.live_env.stat().st_mode & 0o777 == 0o600
    assert "COMPOSE_PROFILES=gateway,embeddings,portal\n" in restored
    assert "GPU_MEM_UTIL=0.85\n" in restored
    # Секреты рабочего клона в релизный не копируются.
    assert not sandbox.env_file.exists()
    # Временный файл рядом с .env рабочего клона не остаётся.
    assert sorted(path.name for path in (sandbox.live / "deploy").iterdir()) == [
        ".env",
        "docker-compose.yml",
        "rollback",
    ]
    # Compose до 0.5 берёт образы без тега: latest обязан указывать на прежний образ.
    assert set(sandbox.images()) >= {f"llm-{service}:latest" for service in PORTAL_SERVICES}

    compose = f"compose -f {sandbox.live}/deploy/docker-compose.yml"
    calls = [
        call for call in sandbox.docker_calls() if " inspect " not in call and " tag " not in call
    ]
    assert calls[-4:] == [
        "systemctl stop llm-stack",
        # Реранкер и песочница неудачного релиза — вне прежних профилей и прежнего compose.
        f"{compose} --profile * down --remove-orphans",
        "stack_up.sh --wait",
        "systemctl start llm-stack",
    ]
    assert not any(call.startswith("backup.sh") for call in calls)


def make_copy(root: Path, name: str = "llm-backup-20261012-030000") -> Path:
    """Завершённая копия: дамп базы и контрольные суммы."""
    copy = root / name
    copy.mkdir()
    dump = b"dump"
    (copy / "portal-db.dump").write_bytes(dump)
    (copy / "SHA256SUMS").write_text(f"{hashlib.sha256(dump).hexdigest()}  portal-db.dump\n")
    return copy


def test_rollback_with_copy_restores_data_before_portal_starts(
    sandbox: ReleaseSandbox, tmp_path: Path
) -> None:
    sandbox.save_previous_env()
    sandbox.set_images(*previous_images())
    copy = make_copy(tmp_path)
    result = sandbox.rollback(str(copy), stdin="yes\n")
    assert result.returncode == 0, result.stderr
    calls = sandbox.docker_calls()
    down = next(index for index, call in enumerate(calls) if " down " in call)
    assert calls[down + 1 :] == [
        f"compose -f {sandbox.live}/deploy/docker-compose.yml up -d --wait portal-db",
        f"backup.sh restore {copy}",
        "stack_up.sh --wait",
        "systemctl start llm-stack",
    ]


def assert_rollback_refused_untouched(
    sandbox: ReleaseSandbox, *fragments: str, copy: Path | None = None, stdin: str = ""
) -> None:
    before = sandbox.live_env.read_text()
    args = [str(copy)] if copy else []
    assert_refused(sandbox.rollback(*args, stdin=stdin), *fragments)
    assert sandbox.live_env.read_text() == before
    assert not any(call.startswith(("systemctl", "stack_up")) for call in sandbox.docker_calls())
    assert not any(" down " in call for call in sandbox.docker_calls())


def test_rollback_is_refused_while_clone_is_on_the_failed_release(sandbox: ReleaseSandbox) -> None:
    sandbox.save_previous_env()
    sandbox.set_images(*previous_images())
    # Compose нового релиза требует переменных, которых в прежнем .env нет.
    (sandbox.stubs / "reject-env").touch()
    assert_rollback_refused_untouched(sandbox, "не подходит", "git checkout")


@pytest.mark.parametrize("problem", ["parent directory", "partial", "checksum", "no answer"])
def test_rollback_with_unusable_copy_is_refused_before_the_stack_stops(
    sandbox: ReleaseSandbox, tmp_path: Path, problem: str
) -> None:
    """Копию проверяет и backup.sh, но уже при остановленном стеке."""
    sandbox.save_previous_env()
    sandbox.set_images(*previous_images())
    copy, stdin, message = make_copy(tmp_path), "yes\n", "откат отменён"
    if problem == "parent directory":
        copy, message = tmp_path, "не похож на завершённую копию"
    elif problem == "partial":
        copy, message = make_copy(tmp_path, "llm-backup-20261012-040000.partial"), "незавершённая"
    elif problem == "checksum":
        (copy / "portal-db.dump").write_bytes(b"damaged")
        message = "контрольные суммы"
    else:
        stdin = "y\n"
    assert_rollback_refused_untouched(sandbox, message, copy=copy, stdin=stdin)
    assert not any(call.startswith("backup.sh") for call in sandbox.docker_calls())


def test_failed_restore_tells_that_the_stack_is_stopped(
    sandbox: ReleaseSandbox, tmp_path: Path
) -> None:
    sandbox.save_previous_env()
    sandbox.set_images(*previous_images())
    (sandbox.live / "scripts" / "backup.sh").write_text("exit 1\n")
    copy = make_copy(tmp_path)
    result = sandbox.rollback(str(copy), stdin="yes\n")
    assert_refused(result, "стек остановлен", f"make release-rollback LIVE={sandbox.live} FROM=")
    assert "stack_up.sh --wait" not in sandbox.docker_calls()


def test_rollback_takes_the_env_copy_saved_for_the_commit_of_the_clone(
    sandbox: ReleaseSandbox,
) -> None:
    """Копий несколько, если в окне ставилось исправление: у каждого релиза своя."""
    sandbox.save_previous_env()
    rollback = sandbox.live / "deploy" / "rollback"
    for tag, share in ((PREVIOUS_COMMIT, "0.84"), ("ffffffffffff", "0.81")):
        (rollback / tag).mkdir()
        (rollback / tag / ".env").write_text(f"GPU_MEM_UTIL={share}\n")
    sandbox.set_images(*previous_images())
    assert sandbox.rollback().returncode == 0
    expected = f"GPU_MEM_UTIL=0.84\nPORTAL_IMAGE_TAG={PREVIOUS_COMMIT}\n"
    assert sandbox.live_env.read_text() == expected


def test_rollback_is_refused_without_env_copy_for_the_commit_of_the_clone(
    sandbox: ReleaseSandbox,
) -> None:
    sandbox.write_live_env(FAILED_RELEASE_ENV)
    other = sandbox.live / "deploy" / "rollback" / "aaaaaaaaaaaa"
    other.mkdir(parents=True)
    (other / ".env").write_text("GPU_MEM_UTIL=0.85\n")
    sandbox.set_images(*previous_images())
    assert_rollback_refused_untouched(sandbox, "копии .env до релиза", "git checkout")


def test_rollback_is_refused_without_tagged_images(sandbox: ReleaseSandbox) -> None:
    sandbox.save_previous_env()
    sandbox.set_images(*previous_images()[:-1])
    assert_rollback_refused_untouched(sandbox, f"нет образа llm-portal-web:{PREVIOUS_COMMIT}")


# --- репетиция миграций ---------------------------------------------------------

REMOVED = ["rm -f -v llm-rehearsal-db", "network rm llm-rehearsal"]


def test_rehearsal_migrates_a_copy_and_removes_it(sandbox: ReleaseSandbox, tmp_path: Path) -> None:
    sandbox.set_images(f"llm-portal-api:{COMMIT}")
    result = sandbox.release("rehearse", str(sandbox.live))
    assert result.returncode == 0, result.stderr
    calls = sandbox.docker_calls()
    live = f"compose -f {sandbox.live}/deploy/docker-compose.yml"
    database = next(call for call in calls if call.startswith("run -d"))
    # Сеть без выхода наружу; база под именем, по которому её ищет образ портала.
    assert "network create --internal llm-rehearsal" in calls
    assert "--network llm-rehearsal --network-alias portal-db" in database
    # Пароль передаётся именем переменной: значения в аргументах нет.
    assert database.endswith("-e POSTGRES_PASSWORD sha256:c-portal-db")
    dump = calls.index(f"{live} exec -T portal-db pg_dump -U portal -d portal --format=custom")
    image = f"llm-portal-api:{COMMIT}"
    migrate = calls.index(
        f"run --rm --network llm-rehearsal -e PORTAL_DB_PASSWORD {image} portal migrate"
    )
    assert dump < migrate
    assert calls[-2:] == REMOVED
    assert "репетиция миграций прошла" in result.stdout


def test_failed_rehearsal_reports_and_still_removes_the_copy(
    sandbox: ReleaseSandbox, tmp_path: Path
) -> None:
    sandbox.set_images(f"llm-portal-api:{COMMIT}")
    (sandbox.stubs / "fail-migrate").touch()
    result = sandbox.release("rehearse", str(sandbox.live))
    assert_refused(result, "миграции релиза", "релиз не выкатывать")
    assert sandbox.docker_calls()[-2:] == REMOVED


def test_rehearsal_needs_prepared_release_image(sandbox: ReleaseSandbox, tmp_path: Path) -> None:
    result = sandbox.release("rehearse", str(sandbox.live))
    assert_refused(result, f"нет образа llm-portal-api:{COMMIT}", "make release-prepare")
    assert not any(call.startswith(("run", "network")) for call in sandbox.docker_calls())
