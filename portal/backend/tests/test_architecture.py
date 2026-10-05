"""Границы слоёв: домен и сценарии не зависят от фреймворков (docs/portal-api.md §13.2)."""

import ast
from pathlib import Path

import pytest

SOURCE = Path(__file__).parent.parent / "src" / "portal"
FORBIDDEN = {
    "fastapi", "starlette", "sqlalchemy", "httpx", "asyncpg", "alembic", "uvicorn",
    "argon2", "cryptography", "pyotp", "segno", "pypdfium2", "docx", "PIL",
}  # fmt: skip
INNER_MODULES = [
    "auth/domain.py",
    "auth/errors.py",
    "auth/ports.py",
    "auth/throttle.py",
    "auth/service.py",
    "auth/admin.py",
    "llm/ports.py",
    "llm/estimator.py",
    "files/ports.py",
    "dialogs/domain.py",
    "dialogs/errors.py",
    "dialogs/ports.py",
    "dialogs/context.py",
    "dialogs/service.py",
    "dialogs/generation.py",
    "core/events.py",
    "core/validation.py",
    "core/errors.py",
    "core/ports.py",
    "core/pagination.py",
    "core/clock.py",
]
# Сценарии зависят от портов, а не от их реализаций.
INFRASTRUCTURE = {
    "portal.auth.repositories", "portal.auth.crypto", "portal.auth.tables", "portal.auth.totp",
    "portal.auth.routes", "portal.auth.admin_routes", "portal.auth.schemas",
    "portal.core.db", "portal.core.audit", "portal.core.app", "portal.core.access",
    "portal.core.container", "portal.core.middleware", "portal.core.sse",
    "portal.llm.bifrost", "portal.files.reader", "portal.files.storage",
    "portal.dialogs.repositories", "portal.dialogs.tables", "portal.dialogs.routes",
    "portal.dialogs.schemas",
}  # fmt: skip


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.parametrize("module", INNER_MODULES)
def test_inner_layers_do_not_import_frameworks_or_infrastructure(module: str) -> None:
    imports = _imports(SOURCE / module)
    assert not {name.split(".")[0] for name in imports} & FORBIDDEN
    assert not imports & INFRASTRUCTURE


def test_source_has_no_print_calls() -> None:
    for path in SOURCE.rglob("*.py"):
        calls = [
            node
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
        ]
        assert not calls, path
