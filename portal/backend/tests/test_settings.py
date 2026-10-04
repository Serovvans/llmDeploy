"""Класс настроек: объединение YAML, файл-накладка, секреты окружения."""

import base64
from pathlib import Path

import pytest
import yaml

from portal.core.settings import ConfigError, load_settings
from tests.conftest import CONFIG_DIR, SECRET_KEY

ENVIRON = {"PORTAL_DB_PASSWORD": "db-secret", "PORTAL_SECRET_KEY": SECRET_KEY}
NO_OVERRIDE = Path("/nonexistent/config.override.yaml")


def _override(tmp_path: Path, content: dict[str, object]) -> Path:
    path = tmp_path / "config.override.yaml"
    path.write_text(yaml.safe_dump(content, allow_unicode=True), encoding="utf-8")
    return path


def test_shipped_configuration_has_contract_start_values() -> None:
    settings = load_settings(ENVIRON, CONFIG_DIR, NO_OVERRIDE)
    auth = settings.auth
    assert settings.database.host == "portal-db"
    assert auth.password.min_length == 12
    assert (auth.session.absolute_ttl_hours, auth.session.idle_ttl_minutes) == (12, 60)
    assert (auth.session.login_step_ttl_minutes, auth.session.stream_recheck_seconds) == (20, 3)
    assert (auth.lockout.max_failures, auth.lockout.base_seconds) == (5, 60)
    assert (auth.lockout.max_seconds, auth.lockout.reset_after_minutes) == (3600, 1440)
    assert (auth.ip_limit.max_failures, auth.ip_limit.window_seconds) == (20, 300)
    assert (auth.totp.window_steps, auth.totp.backup_codes_count) == (1, 10)
    assert auth.totp.issuer == "Портал сотрудников"
    assert "qwertyuiop123" in settings.common_passwords
    assert all(len(password) >= 12 for password in settings.common_passwords)


def test_secrets_are_not_shown_in_repr() -> None:
    settings = load_settings(ENVIRON, CONFIG_DIR, NO_OVERRIDE)
    assert "db-secret" not in repr(settings)
    assert SECRET_KEY not in repr(settings)
    assert settings.require_secret_key() == bytes(range(32))


def test_override_merges_dicts_and_replaces_lists(tmp_path: Path) -> None:
    override = _override(
        tmp_path,
        {
            "auth": {"lockout": {"base_seconds": 2}},
            "chat": {"attachment_extensions": [".pdf"]},
        },
    )
    settings = load_settings(ENVIRON, CONFIG_DIR, override)
    assert settings.auth.lockout.base_seconds == 2
    assert settings.auth.lockout.max_failures == 5
    assert settings.chat.attachment_extensions == (".pdf",)


@pytest.mark.parametrize(
    "content",
    [
        {"auth": {"password": {"min_length": 8}}},
        {"auth": {"unknown_option": 1}},
        {"auth": {"lockout": {"reset_after_minutes": 60}}},
        {"auth": {"lockout": {"max_seconds": 86400}}},
        {"sql": {"default_dialect": "oracle"}},
        {"docparse": {"templates": [{"id": "x", "title": "Без полей и без free_form"}]}},
        {
            "docparse": {
                "templates": [
                    {"id": "x", "title": "И то и другое", "free_form": True,
                     "fields": [{"name": "a", "title": "A"}]}
                ]
            }
        },
        {
            "docparse": {
                "templates": [
                    {"id": "x", "title": "Повтор", "fields": [
                        {"name": "a", "title": "A"}, {"name": "a", "title": "B"}]}
                ]
            }
        },
        {"docparse": {"templates": [{"id": "x", "title": "Имя", "fields": [
            {"name": "кириллица", "title": "A"}]}]}},
    ],
)  # fmt: skip
def test_invalid_configuration_stops_startup(tmp_path: Path, content: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        load_settings(ENVIRON, CONFIG_DIR, _override(tmp_path, content))


def test_missing_database_password_stops_startup() -> None:
    with pytest.raises(ConfigError, match="PORTAL_DB_PASSWORD"):
        load_settings({"PORTAL_SECRET_KEY": SECRET_KEY}, CONFIG_DIR, NO_OVERRIDE)


@pytest.mark.parametrize(
    "key", ["не base64!", base64.b64encode(b"short").decode(), base64.b64encode(bytes(33)).decode()]
)
def test_malformed_secret_key_stops_startup(key: str) -> None:
    with pytest.raises(ConfigError, match="PORTAL_SECRET_KEY") as raised:
        load_settings({**ENVIRON, "PORTAL_SECRET_KEY": key}, CONFIG_DIR, NO_OVERRIDE)
    assert key not in str(raised.value)


def test_secret_key_is_required_only_where_it_is_used() -> None:
    settings = load_settings({"PORTAL_DB_PASSWORD": "x"}, CONFIG_DIR, NO_OVERRIDE)
    with pytest.raises(ConfigError, match="PORTAL_SECRET_KEY"):
        settings.require_secret_key()


def test_empty_config_directory_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(ENVIRON, tmp_path, NO_OVERRIDE)
