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
# ffmpeg и ffprobe для записей (docs/portal-design.md §8.1): воркер запускает их на
# недоверенных файлах, поэтому версия закреплена и обновляется вместе с образом.
# Статическая сборка — два файла без зависимостей: пакет ffmpeg из Debian потянул бы в
# общий с portal-api образ сотни библиотек. ONNX Runtime приходит из uv.lock.
# Дайджест индекса: тег стороннего образа можно перевыложить с другим содержимым, а
# сборка должна брать ровно те файлы, что проверялись. Меняется вместе с версией.
COPY --from=mwader/static-ffmpeg:9.0.2-1@sha256:f5d100184884eb3e82b0eb14bb5ebf98da99826ac824989720297fb9ef21c822 /ffmpeg /ffprobe /usr/local/bin/
COPY alembic.ini ./
COPY config/ config/
COPY migrations/ migrations/
ENV PATH="/app/.venv/bin:${PATH}"
USER portal
# exec: сигнал остановки получает сам сервер, а не оболочка.
CMD ["sh", "-c", "portal migrate && exec portal serve"]
