"""Проверки согласованности параметров базы знаний при старте (docs/portal-api.md §13.6)."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from portal.core.settings import KbSettings, MediaAsrSettings, Settings, load_settings
from tests.conftest import CONFIG_DIR


def _kb(settings: Settings, **sections: dict[str, Any]) -> KbSettings:
    values = settings.kb.model_dump()
    for section, changes in sections.items():
        values[section].update(changes)
    return KbSettings.model_validate(values)


def test_start_values_match_contract_and_fit_together(settings: Settings) -> None:
    kb = settings.kb
    assert kb.search_text_max_chars == 2122
    assert (kb.tags.max_per_document, kb.tags.max_length, kb.tags.max_selected) == (10, 50, 20)
    assert (kb.rerank.candidates, kb.search.neighbors, kb.worker.chat_slots) == (40, 1, 1)
    assert (settings.media.asr.threads, settings.media.asr.chat_threads) == (8, 2)


@pytest.mark.parametrize(
    "sections",
    [
        {"rerank": {"candidates": 41}},  # больше search.prefetch_limit
        {"rerank": {"candidates": 7}},  # меньше search.top_k
        {"rerank": {"document_max_chars": 2121}},  # реранкер получил бы обрезанный фрагмент
        {"embeddings": {"max_input_chars": 2121}},  # то же — эмбеддинги
        {"context": {"max_chars": 501}, "rerank": {"document_max_chars": 2122}},
        {"chunking": {"heading_patterns": ["^(Глава"]}},
    ],
)
def test_inconsistent_values_do_not_let_the_service_start(
    settings: Settings, sections: dict[str, dict[str, Any]]
) -> None:
    with pytest.raises(ValidationError):
        _kb(settings, **sections)


def test_chat_threads_must_leave_threads_for_knowledge_base(settings: Settings) -> None:
    asr = settings.media.asr.model_dump()
    with pytest.raises(ValidationError):
        MediaAsrSettings.model_validate({**asr, "chat_threads": asr["threads"]})


def test_reranker_uses_portal_key_unless_its_own_is_given(
    environ: dict[str, str], override_path: Path
) -> None:
    """Запасная схема доступа к реранкеру (§12.1): необязательная `PORTAL_RERANK_API_KEY`."""
    usual = load_settings(environ, CONFIG_DIR, override_path)
    assert usual.require_rerank_api_key() == environ["PORTAL_LLM_API_KEY"]
    spare = load_settings(
        {**environ, "PORTAL_RERANK_API_KEY": "sk-rerank"}, CONFIG_DIR, override_path
    )
    assert spare.require_rerank_api_key() == "sk-rerank"
    assert "sk-rerank" not in repr(spare)
