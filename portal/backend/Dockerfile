# Образ portal-api (и, с этапа 4, portal-worker — тот же образ с другой командой).

FROM python:3.12.15-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /usr/local/bin/uv
# Байт-код собирается здесь: пользователь итогового образа в /app писать не может.
# Python берётся из базового образа: окружение переносится в такой же.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Зависимости отдельным слоем: правка кода не переустанавливает их.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src/ src/
# Не editable: пакет ставится в окружение, и каталог src в итоговый образ не нужен.
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12.15-slim-bookworm
# Каталог создаётся с владельцем заранее: новый volume portal-files наследует его права.
RUN useradd --system --user-group --uid 10001 --no-create-home portal \
    && install -d -o portal -g portal /data/files
# config/, alembic.ini и migrations/ бэкенд ищет относительно рабочего каталога.
WORKDIR /app
COPY --from=build /app/.venv .venv
COPY alembic.ini ./
COPY config/ config/
COPY migrations/ migrations/
ENV PATH="/app/.venv/bin:${PATH}"
USER portal
# exec: сигнал остановки получает сам сервер, а не оболочка.
CMD ["sh", "-c", "portal migrate && exec portal serve"]
