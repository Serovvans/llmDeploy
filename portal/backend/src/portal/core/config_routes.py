"""`GET /api/config`: значения конфигурации для интерфейса (docs/portal-api.md §4)."""

from typing import Any

from fastapi import APIRouter

from portal.core.access import AnySession, ContainerDep

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
        },
    }
