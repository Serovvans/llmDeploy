"""Подсети интерфейсов `portal-api` — «подсети портала» (docs/portal-api.md §7.6.5, шаг 2).

Подсети сетей compose назначает Docker, заранее их никто не знает, поэтому портал читает
их сам при старте: IPv4 — маршруты подключённых сетей из `/proc/net/route` (шлюз `0`,
маска не `0`), IPv6 — адреса и длины префиксов из `/proc/net/if_inet6`. Петлевой
интерфейс не учитывается.
"""

import ipaddress
import socket
import struct
from pathlib import Path

from portal.databases.hosts import IpNetwork

_PROC = Path("/proc/net")
_LOOPBACK = "lo"


class SubnetsUnavailableError(Exception):
    """Подсети интерфейсов не прочитались."""


def _ipv4(hex_value: str) -> ipaddress.IPv4Address:
    """Адрес из `/proc/net/route`: 32 бита в порядке байтов узла, шестнадцатерично."""
    return ipaddress.IPv4Address(socket.inet_ntoa(struct.pack("=I", int(hex_value, 16))))


def _route_subnets(text: str) -> list[IpNetwork]:
    subnets: list[IpNetwork] = []
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[0] == _LOOPBACK:
            continue
        destination, gateway, mask = _ipv4(fields[1]), _ipv4(fields[2]), _ipv4(fields[7])
        if int(gateway) != 0 or int(mask) == 0:
            continue
        subnets.append(ipaddress.IPv4Network(f"{destination}/{mask}", strict=False))
    return subnets


def _inet6_subnets(text: str) -> list[IpNetwork]:
    subnets: list[IpNetwork] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 6 or fields[5] == _LOOPBACK:
            continue
        address = ipaddress.IPv6Address(int(fields[0], 16))
        subnets.append(ipaddress.IPv6Network(f"{address}/{int(fields[2], 16)}", strict=False))
    return subnets


def interface_subnets(proc: Path = _PROC) -> tuple[IpNetwork, ...]:
    """Подсети всех интерфейсов, кроме петлевого; не прочитались или пусто — исключение.

    Нет `if_inet6` — IPv6 в ядре выключен, IPv6-подсетей нет.
    """
    try:
        subnets = _route_subnets((proc / "route").read_text(encoding="ascii"))
        inet6 = proc / "if_inet6"
        if inet6.exists():
            subnets += _inet6_subnets(inet6.read_text(encoding="ascii"))
    except (OSError, ValueError) as error:
        raise SubnetsUnavailableError(type(error).__name__) from None
    if not subnets:
        raise SubnetsUnavailableError("no subnets")
    return tuple(dict.fromkeys(subnets))
