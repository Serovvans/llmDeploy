"""Ограничение перебора по логину и по адресу (docs/portal-api.md §2.5)."""

import math
from datetime import datetime, timedelta

from portal.auth.domain import ThrottleEntry
from portal.auth.ports import ThrottleRepository
from portal.core.settings import AuthSettings

_MAX_DOUBLINGS = 32


def _seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


class Throttle:
    """Правила счётчиков поверх хранилища; одна транзакция — один экземпляр."""

    def __init__(
        self, repository: ThrottleRepository, settings: AuthSettings, now: datetime
    ) -> None:
        """Запомнить хранилище, пределы из конфигурации и текущее время."""
        self._repository = repository
        self._lockout = settings.lockout
        self._ip_limit = settings.ip_limit
        self._now = now

    async def ip_blocked_for(self, ip: str | None) -> int | None:
        """Сколько секунд адресу ждать конца окна; `None` — предел не достигнут."""
        if ip is None:
            return None
        entry = await self._repository.get("ip", ip)
        if entry is None or entry.failures < self._ip_limit.max_failures:
            return None
        window_end = entry.first_failure_at + timedelta(seconds=self._ip_limit.window_seconds)
        return _seconds_until(window_end, self._now) if window_end > self._now else None

    async def login_locked_for(self, key: str) -> int | None:
        """Сколько секунд осталось до конца блокировки логина; `None` — не заблокирован."""
        entry = await self._current_login_entry(key)
        if entry is None or entry.locked_until is None or entry.locked_until <= self._now:
            return None
        return _seconds_until(entry.locked_until, self._now)

    async def record_failure(self, key: str, ip: str | None) -> int | None:
        """Учесть неудачу по логину и адресу; вернуть срок блокировки, если она наступила."""
        entry = await self._current_login_entry(key) or ThrottleEntry(
            scope="login", key=key, failures=0, first_failure_at=self._now,
            last_failure_at=self._now, locked_until=None,
        )  # fmt: skip
        entry.failures += 1
        entry.last_failure_at = self._now
        over = entry.failures - self._lockout.max_failures
        seconds = None
        if over >= 0:
            seconds = min(
                self._lockout.base_seconds * 2 ** min(over, _MAX_DOUBLINGS),
                self._lockout.max_seconds,
            )
            entry.locked_until = self._now + timedelta(seconds=seconds)
        await self._repository.save(entry)
        await self.record_ip_failure(ip)
        # Чистка — после своих записей: к этому моменту свои строки уже заняты в едином
        # порядке (логин, затем адрес), а занятые чужими транзакциями чистка пропускает.
        await self._delete_stale()
        return seconds

    async def record_ip_failure(self, ip: str | None) -> None:
        """Учесть неудачу только по адресу (отказ `account_blocked`)."""
        if ip is not None:
            window_start = self._now - timedelta(seconds=self._ip_limit.window_seconds)
            await self._repository.add_ip_failure(ip, self._now, window_start)

    async def reset_login(self, key: str) -> None:
        """Обнулить счётчик логина: пройдены все настроенные факторы."""
        await self._repository.delete("login", key)

    async def _current_login_entry(self, key: str) -> ThrottleEntry | None:
        entry = await self._repository.get("login", key)
        if entry is None:
            return None
        locked = entry.locked_until is not None and entry.locked_until > self._now
        reset_at = entry.last_failure_at + timedelta(minutes=self._lockout.reset_after_minutes)
        return entry if locked or reset_at > self._now else None

    async def _delete_stale(self) -> None:
        keep = max(
            timedelta(minutes=self._lockout.reset_after_minutes),
            timedelta(seconds=self._ip_limit.window_seconds),
        )
        await self._repository.delete_stale(self._now - keep, self._now)
