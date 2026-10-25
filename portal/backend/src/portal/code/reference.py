"""Индекс SDK CoGIS для портала: `manifest.json` и `reference.json` (docs/portal-api.md §7.5.14).

Файлы читаются при первом обращении и перечитываются, когда меняется время их изменения:
после `make cogis-sdk` перезапуск портала не нужен. Нет индекса или он не читается —
справочника нет (`available: false`), остальное работает.
"""

import hashlib
import json
import logging
import threading
from typing import Any

from portal.code.domain import ReferenceMember, ReferenceType, SdkSnapshot
from portal.code.zipio import REQUIRED_DLLS
from portal.core.settings import CodeSettings

logger = logging.getLogger(__name__)

_ETAG_BYTES = 16


def _members(raw: Any) -> tuple[ReferenceMember, ...]:
    return tuple(
        ReferenceMember(str(item["kind"]), str(item["name"]), str(item["signature"]))
        for item in raw
    )


def _types(raw: Any) -> list[ReferenceType]:
    return [
        ReferenceType(
            namespace=str(item["namespace"]),
            name=str(item["name"]),
            kind=str(item["kind"]),
            origin=str(item["origin"]),
            signature=str(item["signature"]),
            members=_members(item["members"]),
        )
        for item in raw
    ]


def _type_data(item: ReferenceType) -> dict[str, Any]:
    return {
        "namespace": item.namespace,
        "name": item.name,
        "kind": item.kind,
        "origin": item.origin,
        "signature": item.signature,
        "members": [
            {"kind": member.kind, "name": member.name, "signature": member.signature}
            for member in item.members
        ],
    }


class SdkIndex:
    """Чтение индекса SDK из `code.sdk_index_dir` с перечитыванием по времени изменения."""

    def __init__(self, settings: CodeSettings) -> None:
        """Запомнить каталог индекса, офлайн-набор и частые типы .NET."""
        self._settings = settings
        self._directory = settings.sdk_index_dir
        self._lock = threading.Lock()
        self._stamp: tuple[float, float] | bool | None = False  # False — ещё не читали
        self._snapshot = self._build(None, None)

    def snapshot(self) -> SdkSnapshot:
        """Текущее состояние; блокирующее чтение файлов — вызывается в потоке."""
        with self._lock:
            stamp = self._stamps()
            if stamp != self._stamp:
                self._stamp = stamp
                self._snapshot = self._load() if stamp is not None else self._build(None, None)
            return self._snapshot

    def _stamps(self) -> tuple[float, float] | None:
        try:
            return (
                (self._directory / "manifest.json").stat().st_mtime,
                (self._directory / "reference.json").stat().st_mtime,
            )
        except OSError:
            return None

    def _load(self) -> SdkSnapshot:
        try:
            manifest = json.loads((self._directory / "manifest.json").read_bytes())
            reference = json.loads((self._directory / "reference.json").read_bytes())
            return self._build(manifest, reference)
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("sdk index unreadable", extra={"error_type": type(error).__name__})
            return self._build(None, None)

    def _build(self, manifest: Any, reference: Any) -> SdkSnapshot:
        available = manifest is not None and reference is not None
        libraries: dict[str, dict[str, Any]] = {}
        references: tuple[str, ...] = REQUIRED_DLLS
        types: list[ReferenceType] = []
        sdk_version = None
        if available:
            sdk_version = str(manifest["sdk_version"])
            files = manifest["files"]
            for item in files:
                version = item.get("file_version") or item.get("assembly_version")
                name = str(item["file_name"])
                libraries.setdefault(name.lower(), {"file_name": name, "version": version})
            for name in manifest["plugins"]:
                libraries.setdefault(str(name).lower(), {"file_name": str(name), "version": None})
            references = tuple(
                str(item["file_name"]) for item in files if item["kind"] != "framework"
            )
            types = _types(reference["types"])
        frequent = set(self._settings.editor_dotnet_types)
        editor = [
            item
            for item in types
            if item.origin == "cogis" or (item.origin == "dotnet" and item.full_name in frequent)
        ]
        body = {
            "available": available,
            "sdk_version": sdk_version,
            "libraries": [libraries[key] for key in sorted(libraries)],
            "packages": [
                {
                    "id": package.id,
                    "version": package.version,
                    "on_server": f"{package.id.lower()}.dll" in libraries,
                }
                for package in self._settings.packages
            ],
            "types": [_type_data(item) for item in editor],
        }
        content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
        return SdkSnapshot(
            available=available,
            sdk_version=sdk_version,
            types=tuple(item for item in types if item.origin == "cogis") if available else None,
            references=references,
            libraries=tuple(item["file_name"] for item in libraries.values()),
            body=content,
            etag=f'"{hashlib.sha256(content).digest()[:_ETAG_BYTES].hex()}"',
        )
