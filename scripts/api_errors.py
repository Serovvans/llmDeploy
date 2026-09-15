"""Разбор тел ошибок HTTP API, общий для smoke_test.py и keys.py.

Bifrost отдаёт ошибки в виде ``{"type": ..., "status_code": ..., "error": {"message": ...}}``,
vLLM (OpenAI-совместимо) — ``{"error": {"message": ..., "type": ...}}``.
"""

import json

import httpx

_MAX_BODY_CHARS = 500


def describe_error(response: httpx.Response) -> str:
    """Вернуть краткое описание ошибки: код HTTP, тип и сообщение из тела ответа."""
    prefix = f"HTTP {response.status_code}"
    try:
        body = response.json()
    except json.JSONDecodeError:
        return f"{prefix}: {response.text[:_MAX_BODY_CHARS]}"
    if not isinstance(body, dict):
        return f"{prefix}: {str(body)[:_MAX_BODY_CHARS]}"

    error = body.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    error_type = body.get("type") or (error.get("type") if isinstance(error, dict) else None)
    parts = [prefix]
    if error_type:
        parts.append(f"[{error_type}]")
    parts.append(str(message) if message else json.dumps(body, ensure_ascii=False))
    return " ".join(parts)[:_MAX_BODY_CHARS]
