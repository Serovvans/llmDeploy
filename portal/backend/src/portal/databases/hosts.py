"""Запрет внутренних адресов портала сервером подключения (docs/portal-api.md §7.6.2, §7.6.5).

ПД §8.6 п. 4: нельзя указать loopback, имена сервисов портала и адреса его внутренних
сетей. Имена — список `db.forbidden_hosts`; адреса — петлевые, неуказанный, локальные
канала и подсети интерфейсов самого `portal-api`. Частные диапазоны сами по себе не
запрещены: в них стоят базы заказчика.
"""

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


# Формы IPv6 с IPv4 в последних 32 битах, кроме `::ffff:0:0/96` (её даёт `ipv4_mapped`):
# `::ffff:0:a.b.c.d`, `::a.b.c.d` и NAT64 (§7.6.5, «IPv4 внутри IPv6»).
_EMBEDDING_IPV4 = (
    ipaddress.IPv6Network("::ffff:0:0:0/96"),
    ipaddress.IPv6Network("::/96"),
    ipaddress.IPv6Network("64:ff9b::/96"),
)


def _as_ipv4(address: IpAddress) -> IpAddress:
    """IPv4 внутри IPv6 — сам IPv4; `::` и `::1` остаются IPv6 (они запрещены сами)."""
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.is_unspecified or address.is_loopback:
        return address
    if any(address in network for network in _EMBEDDING_IPV4):
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    return address


def host_key(host: str) -> str:
    """Имя для сравнения со списками: без учёта регистра и точки в конце."""
    return host.lower().removesuffix(".")


@dataclass(frozen=True)
class HostPolicy:
    """Какие имена и адреса нельзя использовать сервером подключения."""

    forbidden_names: frozenset[str]
    allowed_internal: frozenset[str]
    subnets: tuple[IpNetwork, ...]

    @classmethod
    def of(
        cls, forbidden: Iterable[str], allowed_internal: Iterable[str], subnets: Iterable[IpNetwork]
    ) -> "HostPolicy":
        """Политика по спискам `db.*` и подсетям интерфейсов портала."""
        return cls(frozenset(forbidden), frozenset(allowed_internal), tuple(subnets))

    def name_forbidden(self, host: str) -> bool:
        """Имя из `db.forbidden_hosts`."""
        return host_key(host) in self.forbidden_names

    def address_forbidden(self, address: IpAddress, host: str) -> bool:
        """Адрес внутри портала (§7.6.5, шаг 2); `host` — введённое имя.

        IPv4 в записи IPv6 (`::ffff:a.b.c.d`, `::ffff:0:a.b.c.d`, `::a.b.c.d`,
        `64:ff9b::a.b.c.d`) проверяется как IPv4. Для имени из `db.allowed_internal_hosts`
        не проверяются только подсети портала.
        """
        address = _as_ipv4(address)
        if address.is_loopback or address.is_unspecified or address.is_link_local:
            return True
        if host_key(host) in self.allowed_internal:
            return False
        return any(address.version == net.version and address in net for net in self.subnets)

    def saved_host_forbidden(self, host: str) -> bool:
        """Проверка при сохранении: имя или адрес как введены, без разрешения имени."""
        if self.name_forbidden(host):
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False
        return self.address_forbidden(address, host)
