"""Журналы: структурированные и без секретов (docs/portal-api.md §13.5)."""

import json
import logging

import pytest

from portal.core.logging import JsonFormatter
from tests.support import NEW_PASSWORD, Portal

pytestmark = pytest.mark.anyio


def test_formatter_emits_one_json_object_with_extra_fields() -> None:
    record = logging.LogRecord("portal", logging.INFO, __file__, 1, "request", None, None)
    record.path = "/api/auth/login"
    record.status = 200
    entry = json.loads(JsonFormatter().format(record))
    assert entry["message"] == "request"
    assert entry["level"] == "info"
    assert (entry["path"], entry["status"]) == ("/api/auth/login", 200)
    assert entry["time"].endswith("+00:00")


async def test_login_flow_leaves_no_secrets_in_logs(
    portal: Portal, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with portal.client() as client:
        account = await portal.onboard(client, "ivanov")
        token = client.cookies["portal_session"]
        await client.get("/api/admin/users", params={"q": "Иванов"})
        await client.post(
            "/api/auth/login", json={"login": "ivanov", "password": "неверный-пароль"}
        )
    formatter = JsonFormatter()
    output = "\n".join(formatter.format(record) for record in caplog.records)
    assert '"message": "request"' in output and "/api/auth/login" in output
    secrets = [
        NEW_PASSWORD,
        "неверный-пароль",
        token,
        account.secret,
        "Иванов",
        *account.backup_codes,
    ]
    assert not [secret for secret in secrets if secret in output]
