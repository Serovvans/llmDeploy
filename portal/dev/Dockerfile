# Заглушка OpenAI-совместимого API для стенда (docs/portal-api.md §12.2).

FROM python:3.12.15-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.15-slim-bookworm
RUN useradd --system --user-group --uid 10001 --no-create-home stub
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:${PATH}"
USER stub
CMD ["uvicorn", "llm_stub.app:app", "--host", "0.0.0.0", "--port", "8080"]
