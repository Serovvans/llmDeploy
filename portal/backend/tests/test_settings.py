"""Класс настроек: объединение YAML, файл-накладка, секреты окружения."""

import base64
from pathlib import Path

import pytest
import yaml

from portal.core.container import build_container
from portal.core.settings import ConfigError, load_settings
from tests.conftest import CONFIG_DIR, SECRET_KEY

ENVIRON = {
    "PORTAL_DB_PASSWORD": "db-secret",
    "PORTAL_SECRET_KEY": SECRET_KEY,
    "PORTAL_LLM_API_KEY": "sk-bf-secret",
    "QDRANT_API_KEY": "qdrant-secret",
    "MAX_MODEL_LEN": "65536",
}
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


def test_chat_start_values_match_the_contract() -> None:
    settings = load_settings(ENVIRON, CONFIG_DIR, NO_OVERRIDE)
    assert settings.server.multipart_overhead_bytes == 65536
    llm = settings.llm
    assert (llm.base_url, llm.chat_model) == ("http://bifrost:8080/v1", "default")
    assert (llm.safety_margin_tokens, llm.first_token_timeout_seconds) == (4096, 120)
    assert (llm.image_max_side_px, llm.context_overflow_marker) == (1568, "maximum context length")
    dialogs = settings.dialogs
    assert (dialogs.generation_timeout_seconds, dialogs.stop_grace_seconds) == (900, 5)
    assert (dialogs.title_wait_seconds, dialogs.title_max_tokens) == (3, 256)
    assert dialogs.keepalive_seconds <= 15
    assert (settings.chat.max_output_tokens.fast, settings.chat.max_output_tokens.thorough) == (
        4096, 16384,
    )  # fmt: skip
    assert str(settings.files.root) == "/data/files"
    assert settings.files.text_layer_min_valid_share == 0.8
    assert settings.require_llm_api_key() == "sk-bf-secret"
    assert settings.require_max_model_len() == 65536
    assert "sk-bf-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PORTAL_LLM_API_KEY", None),
        ("PORTAL_LLM_API_KEY", ""),
        ("PORTAL_LLM_API_KEY", "   "),
        ("MAX_MODEL_LEN", None),
        ("QDRANT_API_KEY", None),
        ("QDRANT_API_KEY", ""),
    ],
)
def test_serve_refuses_to_start_without_model_settings(variable: str, value: str | None) -> None:
    """Без ключа портала, ключа Qdrant и контекста модели не стартует только `serve` (§13.5)."""
    environ = {name: text for name, text in ENVIRON.items() if name != variable}
    if value is not None:
        environ[variable] = value
    settings = load_settings(environ, CONFIG_DIR, NO_OVERRIDE)  # остальным командам достаточно
    with pytest.raises(ConfigError, match=variable):
        build_container(settings)


@pytest.mark.parametrize("value", ["много", "0", "-5", "65536.0"])
def test_malformed_max_model_len_stops_startup(value: str) -> None:
    with pytest.raises(ConfigError, match="MAX_MODEL_LEN"):
        load_settings({**ENVIRON, "MAX_MODEL_LEN": value}, CONFIG_DIR, NO_OVERRIDE)


def test_keepalive_longer_than_contract_allows_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_settings(
            ENVIRON, CONFIG_DIR, _override(tmp_path, {"dialogs": {"keepalive_seconds": 16}})
        )


def test_page_raster_side_must_fit_the_pixel_limit(tmp_path: Path) -> None:
    """Согласованность настроек: растр страницы PDF заведомо меньше предела точек."""
    with pytest.raises(ConfigError, match="image_max_side_px"):
        load_settings(
            ENVIRON, CONFIG_DIR, _override(tmp_path, {"files": {"image_max_pixels": 1_000_000}})
        )


def test_knowledge_base_configuration_has_contract_start_values() -> None:
    """Стартовые значения §13.6 для базы знаний, распознавания и очереди."""
    settings = load_settings(ENVIRON, CONFIG_DIR, NO_OVERRIDE)
    kb, llm = settings.kb, settings.llm
    assert (kb.document_max_bytes, kb.document_max_pages) == (50 * 1024 * 1024, 500)
    assert (kb.search.top_k, kb.search.prefetch_limit, kb.search.lexical_slots) == (8, 40, 3)
    assert (kb.context_max_tokens, kb.embeddings.dimension) == (8000, 1024)
    worker = kb.worker
    assert (worker.concurrency, worker.max_attempts, worker.lease_seconds) == (1, 8, 300)
    assert (worker.retry_base_seconds, worker.retry_max_seconds) == (60, 1800)
    assert (kb.qdrant.url, kb.qdrant.collection) == ("http://qdrant:6333", "kb_fragments")
    assert (llm.embedding_model, llm.recognition_max_tokens) == ("embeddings", 4096)
    assert llm.recognition_parallel_requests == 2
    assert settings.require_qdrant_api_key() == "qdrant-secret"
    assert "qdrant-secret" not in repr(settings)


@pytest.mark.parametrize(
    "kb",
    [
        {"chunking": {"overlap_chars": 700}},
        {"chunking": {"max_chars": 9000}},
        {"worker": {"heartbeat_stale_seconds": 5}},
        {"worker": {"concurrency": 0}},
        {"search": {"top_k": 0}},
        {"search": {"lexical_slots": 9}},
    ],
)
def test_inconsistent_knowledge_base_settings_stop_startup(
    tmp_path: Path, kb: dict[str, object]
) -> None:
    with pytest.raises(ConfigError):
        load_settings(ENVIRON, CONFIG_DIR, _override(tmp_path, {"kb": kb}))
