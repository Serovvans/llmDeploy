"""`GET /api/config`: значения конфигурации для интерфейса (docs/portal-api.md §4)."""

from typing import Any

from fastapi import APIRouter

from portal.core.access import AnySession, ContainerDep
from portal.core.container import Container

# Лимит модели (docs/design.md §6), а не параметр портала.
MAX_IMAGES_PER_REQUEST = 8

router = APIRouter(prefix="/api")


@router.get("/config")
async def get_config(_: AnySession, container: ContainerDep) -> dict[str, Any]:
    """Параметры для подсказок интерфейса; доступно на любом шаге входа."""
    settings = container.settings
    return {
        "password": {
            "min_length": settings.auth.password.min_length,
            "max_length": settings.auth.password.max_length,
        },
        "dialogs": {"message_max_chars": settings.dialogs.message_max_chars},
        "chat": {
            "attachment_max_bytes": settings.chat.attachment_max_bytes,
            "attachment_max_pages": settings.chat.attachment_max_pages,
            "attachment_extensions": settings.chat.attachment_extensions,
            "max_attachments": settings.chat.max_attachments,
            "max_images": MAX_IMAGES_PER_REQUEST,
        },
        "kb": {
            "document_max_bytes": settings.kb.document_max_bytes,
            "document_max_pages": settings.kb.document_max_pages,
            "document_extensions": settings.kb.document_extensions,
            "tags": {
                "max_per_document": settings.kb.tags.max_per_document,
                "max_length": settings.kb.tags.max_length,
                "max_selected": settings.kb.tags.max_selected,
            },
        },
        "media": {
            "enabled": settings.media.enabled,
            "max_bytes": settings.media.max_bytes,
            "max_minutes": settings.media.max_minutes,
            "chat_max_minutes": settings.media.chat_max_minutes,
            "extensions": settings.media.extensions,
        },
        "docparse": {
            "document_max_bytes": settings.docparse.document_max_bytes,
            "max_pages": settings.docparse.max_pages,
            "document_extensions": settings.docparse.document_extensions,
            "templates": [
                {
                    "id": template.id,
                    "title": template.title,
                    "description": template.description,
                    "free_form": template.free_form,
                }
                for template in settings.docparse.templates
            ],
        },
        "sql": {
            "dialects": [
                {"id": dialect.id, "title": dialect.title} for dialect in settings.sql.dialects
            ],
            "default_dialect": settings.sql.default_dialect,
            "schema_max_chars": settings.sql.schema_max_chars,
            "max_schemas": settings.sql.max_schemas,
            "test_run": {
                "enabled": settings.sql.sandbox.enabled,
                "dialect": settings.sql.sandbox.dialect,
            },
        },
        "memory": {
            "enabled": settings.memory.enabled,
            "keep_recent_pairs": settings.memory.keep_recent_pairs,
            "max_chars": settings.memory.max_chars,
        },
        "code": _code(container),
    }


def _code(container: Container) -> dict[str, Any]:
    """Флаги и пределы раздела «Код» (§4, §7.5.3–7.5.6); типы мастера — из шаблонов."""
    code = container.settings.code
    return {
        "enabled": code.enabled,
        "build_enabled": code.build.enabled,
        "file_extensions": list(code.file_extensions),
        "max_projects": code.max_projects,
        "max_files": code.max_files,
        "file_max_bytes": code.file_max_bytes,
        "project_max_bytes": code.project_max_bytes,
        "import_max_bytes": code.import_max_bytes,
        "max_snapshots": code.max_snapshots,
        "fix_attempts": code.fix.attempts,
        "wizard": {
            "max_tools": code.wizard.max_tools,
            "max_parameters": code.wizard.max_parameters,
            "parameter_types": [
                {
                    "id": item.id,
                    "title": item.title,
                    "csharp": item.csharp,
                    "input": item.input,
                    "output": item.output,
                }
                for item in container.code.templates.types
            ],
        },
    }
