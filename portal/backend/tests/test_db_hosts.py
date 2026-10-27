"""Сервер подключения — не внутри портала (docs/portal-api.md §7.6.2, §7.6.5 шаг 2, А-147).

При сохранении проверяются имя и адрес как введены, без DNS; при каждом обращении имя
разрешается один раз, проверяются все адреса, соединение — только на проверенный адрес, TLS
— по введённому имени. Разрешение имён и соединение подменены: сети в тестах нет.
"""

import ipaddress
import ssl
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import certifi
import pytest
import yaml

from portal.core import container as container_module
from portal.core.container import build_container
from portal.core.settings import ConfigError, Settings
from portal.databases.customer import AsyncpgCustomerDatabase
from portal.databases.hosts import HostPolicy, IpNetwork
from portal.databases.network import SubnetsUnavailableError, interface_subnets
from portal.databases.ports import DbTarget, DbUnavailableError
from tests.conftest import close_portal, make_portal
from tests.db_support import admin_client, create_connection, db_settings

pytestmark = pytest.mark.anyio

COMPOSE = Path(__file__).resolve().parents[3] / "deploy" / "docker-compose.yml"
SUBNETS: tuple[IpNetwork, ...] = (
    ipaddress.ip_network("172.18.0.0/16"),
    ipaddress.ip_network("fd00:12::/64"),
)
FORBIDDEN = [
    "portal-db",
    "PORTAL-DB.",
    "localhost",
    "127.0.0.2",
    "::1",
    "::ffff:127.0.0.1",
    "::ffff:0:127.0.0.1",
    "::127.0.0.1",
    "64:ff9b::7f00:1",
    "64:ff9b::a9fe:a9fe",
    "::",
    "0.0.0.0",
    "169.254.169.254",
    "fe80::1",
    "172.18.0.7",
    "::ffff:172.18.0.7",
    "::ffff:0:172.18.0.7",
    "::172.18.0.7",
    "64:ff9b::ac12:7",
    "fd00:12::5",
]
ALLOWED = [
    "10.0.4.12",
    "172.20.1.5",
    "192.168.25.10",
    "::ffff:10.0.4.12",
    "64:ff9b::a00:40c",
    "db.customer.test",
]


class Resolver:
    """Подменное разрешение имён: ответы по очереди, вызовы — в `calls`."""

    def __init__(self, *answers: Sequence[str]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    async def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append(host)
        return list(self.answers[min(len(self.calls), len(self.answers)) - 1])


@pytest.fixture
def connects(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Попытки соединения адаптера: запоминаются и отклоняются, как недоступный сервер.

    Соединения с базой портала (сокет) проходят как есть.
    """
    attempts: list[dict[str, Any]] = []
    connect = asyncpg.connect

    async def refused(*args: Any, **kwargs: Any) -> Any:
        if str(kwargs.get("host", "")).startswith("/"):
            return await connect(*args, **kwargs)
        attempts.append(kwargs)
        raise ConnectionRefusedError

    monkeypatch.setattr(asyncpg, "connect", refused)
    return attempts


def _adapter(settings: Settings, resolver: Resolver, **db: Any) -> AsyncpgCustomerDatabase:
    config = db_settings(settings, **db).db
    policy = HostPolicy.of(config.forbidden_hosts, config.allowed_internal_hosts, SUBNETS)
    return AsyncpgCustomerDatabase(config, policy, resolver)


def _target(host: str, tls_mode: Any = "disable") -> DbTarget:
    return DbTarget(uuid4(), host, 5432, "cadastre", "portal_reader", "x", tls_mode, ("public",))


async def test_forbidden_hosts_are_refused_on_save_and_customer_networks_accepted(
    settings: Settings,
) -> None:
    portal = await make_portal(db_settings(settings), portal_subnets=SUBNETS)
    try:
        admin = await admin_client(portal)
        for host in FORBIDDEN:
            response = await admin.post(
                "/api/admin/db-connections",
                json={
                    "title": host,
                    "host": host,
                    "database": "cadastre",
                    "username": "portal_reader",
                    "password": "secret",
                    "tls_mode": "disable",
                    "schemas": ["public"],
                    "access": {"all": True},
                },
            )
            assert response.status_code == 422, host
            (field,) = response.json()["error"]["fields"]
            assert (field["field"], field["code"]) == ("host", "host_forbidden"), host
            assert (
                field["message"] == "Это внутренний адрес портала. Укажите сервер базы заказчика."
            )
        for number, host in enumerate(ALLOWED):
            await create_connection(admin, title=f"База {number}", host=host)
        # PATCH без host поле не проверяет.
        target = (await create_connection(admin, title="Другая", host="10.0.0.9"))["id"]
        patched = await admin.patch(f"/api/admin/db-connections/{target}", json={"port": 6432})
        assert patched.status_code == 200
        moved = await admin.patch(f"/api/admin/db-connections/{target}", json={"host": "qdrant"})
        assert moved.json()["error"]["fields"][0]["code"] == "host_forbidden"
    finally:
        await close_portal(portal)


async def test_name_resolved_to_internal_address_is_not_connected(
    settings: Settings, connects: list[dict[str, Any]]
) -> None:
    """Имя ведёт на внутренний адрес — `host_forbidden` в `last_error`, соединения нет."""
    resolver = Resolver(["10.0.4.12", "172.18.0.3"])
    adapter = _adapter(settings, resolver)
    portal = await make_portal(db_settings(settings), customer_database=adapter)
    try:
        admin = await admin_client(portal)
        target = (await create_connection(admin))["id"]
        checked = (await admin.post(f"/api/admin/db-connections/{target}/check")).json()
        assert checked["last_error"]["reason"] == "host_forbidden"
        assert connects == []
        assert resolver.calls == ["db.customer.test"]
        audit = await portal.rows(
            "SELECT details FROM audit_log WHERE event = 'db_connection_checked'"
        )
        assert audit[0][0]["rights"] is None
        assert audit[0][0]["reason"] == "host_forbidden"
    finally:
        await close_portal(portal)


async def test_forbidden_name_is_not_resolved(
    settings: Settings, connects: list[dict[str, Any]]
) -> None:
    """Подключение, сохранённое до запрета, получает `host_forbidden` без DNS."""
    resolver = Resolver(["10.0.4.12"])
    with pytest.raises(DbUnavailableError) as raised:
        await _adapter(settings, resolver).check(_target("Portal-DB."))
    assert raised.value.reason == "host_forbidden"
    assert resolver.calls == [] and connects == []


async def test_name_is_resolved_once_and_connection_goes_to_checked_address(
    settings: Settings, connects: list[dict[str, Any]]
) -> None:
    """DNS rebinding: второе разрешение дало бы петлевой адрес — его не бывает."""
    resolver = Resolver(["203.0.113.7", "203.0.113.8"], ["127.0.0.1"])
    with pytest.raises(DbUnavailableError) as raised:
        await _adapter(settings, resolver).check(_target("db.customer.test"))
    assert raised.value.reason == "unreachable"
    assert resolver.calls == ["db.customer.test"]
    assert [attempt["host"] for attempt in connects] == ["203.0.113.7", "203.0.113.8"]


async def test_one_forbidden_address_among_many_refuses_the_name(
    settings: Settings, connects: list[dict[str, Any]]
) -> None:
    resolver = Resolver(["203.0.113.7", "::ffff:127.0.0.1"])
    with pytest.raises(DbUnavailableError) as raised:
        await _adapter(settings, resolver).check(_target("db.customer.test"))
    assert raised.value.reason == "host_forbidden"
    assert connects == []


async def test_no_addresses_is_unreachable(
    settings: Settings, connects: list[dict[str, Any]]
) -> None:
    with pytest.raises(DbUnavailableError) as raised:
        await _adapter(settings, Resolver([])).check(_target("db.customer.test"))
    assert raised.value.reason == "unreachable"
    assert connects == []


async def test_verify_checks_certificate_against_entered_name(
    settings: Settings, connects: list[dict[str, Any]]
) -> None:
    resolver = Resolver(["203.0.113.7"])
    adapter = _adapter(settings, resolver, tls_root_cert_file=Path(certifi.where()))
    with pytest.raises(DbUnavailableError):
        await adapter.check(_target("db.customer.test", "verify"))
    (attempt,) = connects
    assert attempt["host"] == "203.0.113.7"
    context: ssl.SSLContext = attempt["ssl"]
    assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    # asyncpg передаёт имя из `host` — адрес; сертификат проверяется по введённому имени.
    tls = context.wrap_bio(ssl.MemoryBIO(), ssl.MemoryBIO(), server_hostname="203.0.113.7")
    assert tls.server_hostname == "db.customer.test"


@pytest.mark.parametrize(
    ("address", "forbidden"),
    [("172.18.0.5", False), ("127.0.0.1", True), ("169.254.169.254", True), ("::", True)],
)
async def test_allowed_internal_host_lifts_only_the_subnet_rule(
    settings: Settings, connects: list[dict[str, Any]], address: str, forbidden: bool
) -> None:
    adapter = _adapter(settings, Resolver([address]), allowed_internal_hosts=("customer-db",))
    with pytest.raises(DbUnavailableError) as raised:
        await adapter.check(_target("customer-db"))
    assert (raised.value.reason == "host_forbidden") is forbidden
    assert len(connects) == (0 if forbidden else 1)


def test_policy_without_exception_refuses_portal_subnet() -> None:
    policy = HostPolicy.of(["portal-db"], [], SUBNETS)
    assert policy.address_forbidden(ipaddress.ip_address("172.18.0.5"), "customer-db")
    assert not policy.address_forbidden(ipaddress.ip_address("172.19.0.5"), "customer-db")


def test_forbidden_hosts_list_every_compose_service(settings: Settings) -> None:
    """Новый сервис compose без строки в `db.forbidden_hosts` — ошибка (§13.6)."""
    services = set(yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"])
    assert set(settings.db.forbidden_hosts) == services | {"localhost"}
    assert settings.db.allowed_internal_hosts == ()


def test_interface_subnets_from_proc(tmp_path: Path) -> None:
    (tmp_path / "route").write_text(
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        "eth0\t00000000\t010012AC\t0003\t0\t0\t0\t00000000\n"
        "eth0\t000012AC\t00000000\t0001\t0\t0\t0\t0000FFFF\n"
        "eth1\t0000A8C0\t00000000\t0001\t0\t0\t0\t00FFFFFF\n"
        "lo\t0000007F\t00000000\t0001\t0\t0\t0\t000000FF\n",
        encoding="ascii",
    )
    (tmp_path / "if_inet6").write_text(
        "00000000000000000000000000000001 01 80 10 80       lo\n"
        "fd000012000000000000000000000005 02 40 00 80     eth0\n",
        encoding="ascii",
    )
    assert interface_subnets(tmp_path) == (
        ipaddress.ip_network("172.18.0.0/16"),
        ipaddress.ip_network("192.168.0.0/24"),
        ipaddress.ip_network("fd00:12::/64"),
    )
    (tmp_path / "route").unlink()
    with pytest.raises(SubnetsUnavailableError):
        interface_subnets(tmp_path)


def test_unread_subnets_stop_start_only_when_connections_are_enabled(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unreadable() -> tuple[IpNetwork, ...]:
        raise SubnetsUnavailableError("FileNotFoundError")

    monkeypatch.setattr(container_module, "interface_subnets", unreadable)
    with pytest.raises(ConfigError):
        build_container(db_settings(settings))
    build_container(settings)
