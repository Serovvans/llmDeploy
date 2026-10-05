# Однострочные команды развёртывания и эксплуатации LLM-сервиса (docs/design.md §9).
# Обёртки над scripts/ и docker compose: своей логики здесь нет.
#
# Развёртывание идёт этапами (docs/design.md §3.7, §3.8):
#   sudo make host && sudo reboot                          подготовка ВМ
#   make model                                             этап 1: модель (vLLM)
#   make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp    этап 2: внешний доступ
#   make portal                                            этап 3: портал сотрудников
#
# `make help` — список целей. Цели работают с docker, systemd и deploy/.env, поэтому
# сами подставляют sudo, если Makefile запущен не от root.

SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
DEPLOY_DIR := $(ROOT_DIR)/deploy
SCRIPTS_DIR := $(ROOT_DIR)/scripts
PORTAL_DIR := $(ROOT_DIR)/portal
COMPOSE_FILE := $(DEPLOY_DIR)/docker-compose.yml

SUDO := $(shell [ "$$(id -u)" -eq 0 ] || echo sudo)
COMPOSE := $(SUDO) docker compose -f $(COMPOSE_FILE)
INSTALL := $(SUDO) bash $(SCRIPTS_DIR)/install.sh
# Запускает команду в scripts/ с переменными из deploy/.env; секреты не идут в argv.
WITH_ENV := $(SUDO) bash $(SCRIPTS_DIR)/with_env.sh

# Параметры целей: make gateway LLM_HOSTNAME=llm.corp.example TLS_MODE=corp
PROFILES ?=
LLM_HOSTNAME ?=
TLS_MODE ?=
KEY ?=
# При TLS_MODE=internal корень CA выгружается сюда; для corp задайте CA=<PEM корп. CA>.
CA ?= $(wildcard $(DEPLOY_DIR)/caddy-root.crt)
NAME ?=
ID ?=
REQUESTS ?=
TOKENS ?=
SERVICE ?=
CONCURRENCY ?=
# Дополнительные аргументы smoke_test.py, например ARGS="--check-rate-limit 5".
ARGS ?=
# Портал: логин пользователя и отбор журнала аудита (make portal-audit SINCE=2026-10-01).
LOGIN ?=
SINCE ?=
EVENT ?=
LIMIT ?=
ONLY_ERRORS ?=
# Резервная копия: куда писать (make backup DIR=...) и откуда восстанавливать (FROM=...).
DIR ?=
FROM ?=
# Эти значения попадают в команды только через окружение ("$$NAME"), а не подстановкой
# в текст рецепта: апостроф или пробел в ФИО и пути не ломают команду.
export LOGIN NAME SINCE EVENT LIMIT DIR FROM

.PHONY: help host preflight model gateway smoke-model smoke bench key keys revoke \
        portal smoke-portal portal-admin portal-reset-2fa portal-audit portal-reindex \
        portal-reindex-recreate portal-eval-search backup restore \
        up down restart ps logs preload check

help: ## Показать список целей
	@echo "Развёртывание LLM-сервиса. Использование: make <цель> [ПАРАМЕТР=значение]"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN { FS = ":.*?## " } { printf "  %-24s %s\n", $$1, $$2 }'
	@echo
	@echo "Этап 1 (модель):  make preflight && make model [PROFILES=monitoring]"
	@echo "Этап 2 (доступ):  make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp|internal"
	@echo "Этап 3 (портал):  make key NAME=portal, ключ — в PORTAL_LLM_API_KEY в deploy/.env, затем make portal"

# --- подготовка ВМ ---

host: ## Установить драйвер, Docker, NVIDIA Container Toolkit, uv (затем reboot)
	$(SUDO) bash $(SCRIPTS_DIR)/install_host.sh

preflight: ## Проверить готовность ВМ (GPU, драйвер, Docker, сеть, диск)
	$(SUDO) bash $(SCRIPTS_DIR)/preflight.sh

# --- этап 1: модель ---

model: ## Этап 1: развернуть модель (vLLM на 127.0.0.1:8000), без внешнего доступа
	$(INSTALL) --stage model $(if $(PROFILES),--profiles $(PROFILES))

smoke-model: ## Этап 1: смоук-тест модели напрямую (текст, зрение, json_schema, tool call)
	$(WITH_ENV) bash -c 'LLM_API_KEY="$$VLLM_API_KEY" exec uv run smoke_test.py --direct $(ARGS)'

bench: ## Бенчмарк модели: TTFT и throughput (CONCURRENCY=8)
	$(SUDO) bash $(SCRIPTS_DIR)/bench.sh $(CONCURRENCY)

# --- этап 2: внешний доступ ---

gateway: ## Этап 2: добавить Caddy и Bifrost (LLM_HOSTNAME=... TLS_MODE=corp|internal)
	$(INSTALL) --stage gateway --skip-preflight \
		$(if $(LLM_HOSTNAME),--hostname $(LLM_HOSTNAME)) \
		$(if $(TLS_MODE),--tls $(TLS_MODE)) \
		$(if $(PROFILES),--profiles $(PROFILES))

smoke: ## Этап 2: смоук-тест через HTTPS (KEY=sk-bf-... [CA=<PEM корневого CA>])
	@[[ -n "$(KEY)" ]] || { echo "укажите KEY=sk-bf-... (создать: make key NAME=smoke)" >&2; exit 1; }
	$(WITH_ENV) env LLM_API_KEY='$(KEY)' $(if $(CA),LLM_CA_CERT='$(CA)') uv run smoke_test.py $(ARGS)

key: ## Создать ключ клиента (NAME=app-crm [REQUESTS=600 TOKENS=500000])
	@[[ -n "$(NAME)" ]] || { echo "укажите NAME=<имя ключа>" >&2; exit 1; }
	$(WITH_ENV) uv run keys.py create --name '$(NAME)' \
		$(if $(REQUESTS),--requests $(REQUESTS)) $(if $(TOKENS),--tokens $(TOKENS))

keys: ## Показать выданные ключи, их лимиты и расход
	$(WITH_ENV) uv run keys.py list

revoke: ## Отозвать ключ (ID=<id из make keys>)
	@[[ -n "$(ID)" ]] || { echo "укажите ID=<id ключа> (список: make keys)" >&2; exit 1; }
	$(WITH_ENV) uv run keys.py revoke '$(ID)'

# --- этап 3: портал сотрудников ---

portal: ## Этап 3: добавить портал (нужны gateway, embeddings и PORTAL_LLM_API_KEY в deploy/.env)
	$(INSTALL) --stage portal --skip-preflight $(if $(PROFILES),--profiles $(PROFILES))

smoke-portal: ## Этап 3: что отдаёт сайт через 443 и здоровы ли сервисы; без запросов к модели ([CA=...])
	$(WITH_ENV) env $(if $(CA),LLM_CA_CERT='$(CA)') uv run smoke_test.py --site-only --compose-file $(COMPOSE_FILE)

# Временный пароль печатается один раз и только на экран: команда не выводится (@) и
# ничего не пишет в файлы.
portal-admin: ## Создать администратора портала (LOGIN=ivanov NAME="Иванов И. И."); пароль выводится один раз
	@[[ -n "$$LOGIN" && -n "$$NAME" ]] || { echo 'укажите LOGIN=<логин> NAME="<ФИО>"' >&2; exit 1; }
	@$(COMPOSE) exec portal-api portal create-admin --login "$$LOGIN" --full-name "$$NAME"

portal-reset-2fa: ## Сбросить второй фактор пользователя портала (LOGIN=ivanov)
	@[[ -n "$$LOGIN" ]] || { echo "укажите LOGIN=<логин>" >&2; exit 1; }
	$(COMPOSE) exec portal-api portal reset-second-factor --login "$$LOGIN"

# Вывод — только строки журнала (поля через табуляцию), чтобы его можно было передать
# в cut/grep: время, событие, кто, над кем, адрес, details в JSON.
portal-audit: ## Журнал аудита портала ([SINCE=2026-10-01] [EVENT=login_failed] [LIMIT=100])
	@$(COMPOSE) exec -T portal-api portal audit $(if $(SINCE),--since "$$SINCE") \
		$(if $(EVENT),--event "$$EVENT") $(if $(LIMIT),--limit "$$LIMIT")

portal-reindex: ## Вернуть документы базы знаний в очередь ([ONLY_ERRORS=1] — только с ошибкой)
	$(COMPOSE) exec -T portal-worker portal reindex $(if $(ONLY_ERRORS),--only-errors)

# Пересоздание коллекции допустимо только при остановленном воркере (docs/portal-api.md
# §13.4); воркер запускается обратно и при отказе команды, и при Ctrl-C.
portal-reindex-recreate: ## Пересоздать коллекцию Qdrant и переиндексировать всё (смена модели эмбеддингов)
	@trap '$(COMPOSE) up -d portal-worker' EXIT; set -x; \
		$(COMPOSE) stop portal-worker && \
		$(COMPOSE) run --rm portal-worker portal reindex --recreate-collection

portal-eval-search: ## Оценить поиск базы знаний на контрольном наборе
	$(COMPOSE) exec -T portal-worker portal eval-search

# --- эксплуатация ---

backup: ## Резервная копия: база и файлы портала, векторы, данные Bifrost (DIR=<каталог>)
	@[[ -n "$$DIR" ]] || { echo "укажите DIR=<каталог для копий, вне репозитория>" >&2; exit 1; }
	$(SUDO) bash $(SCRIPTS_DIR)/backup.sh create "$$DIR"

restore: ## Восстановить данные из копии, заменив текущие (FROM=<каталог копии>)
	@[[ -n "$$FROM" ]] || { echo "укажите FROM=<каталог копии llm-backup-...>" >&2; exit 1; }
	$(SUDO) bash $(SCRIPTS_DIR)/backup.sh restore "$$FROM"

up: ## Запустить стек и дождаться healthy
	$(SUDO) systemctl start llm-stack
	$(COMPOSE) up -d --wait

down: ## Остановить стек (systemd-юнит вместе с ним)
	$(SUDO) systemctl stop llm-stack

restart: ## Перезапустить стек
	$(SUDO) systemctl restart llm-stack

ps: ## Состояние контейнеров
	$(COMPOSE) ps

logs: ## Логи (SERVICE=vllm|bifrost|caddy|portal-api|...; по умолчанию все)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

preload: ## Докачать веса MODEL_ID в volume hf-cache (после смены модели)
	$(WITH_ENV) bash -c 'exec docker compose -f $(COMPOSE_FILE) run --rm --no-deps \
		-e HF_HUB_OFFLINE=0 --entrypoint hf vllm download "$$MODEL_ID"'

# --- разработка (локально, без GPU) ---

# Запускать без sudo: тесты бэкенда портала поднимают PostgreSQL, а он от root не стартует.
# Первому запуску нужна сеть (uv sync, npm ci).
check: ## Локальные проверки: docker compose config, ruff, mypy, pytest, shellcheck, портал
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
		sed -e 's/^\([A-Za-z_]*\)=$$/\1=placeholder/' $(DEPLOY_DIR)/.env.example >"$$tmp"; \
		for profiles in "" "gateway" "gateway,embeddings,monitoring" \
				"gateway,embeddings,portal" "gateway,embeddings,monitoring,portal"; do \
			echo "==> docker compose config COMPOSE_PROFILES=$$profiles"; \
			COMPOSE_PROFILES="$$profiles" docker compose -f $(COMPOSE_FILE) --env-file "$$tmp" config -q || exit 1; \
		done; \
		echo "==> docker compose config portal/dev (стенд)"; \
		sed -e 's/^\([A-Za-z_]*\)=$$/\1=placeholder/' $(PORTAL_DIR)/dev/.env.example >"$$tmp"; \
		docker compose -f $(PORTAL_DIR)/dev/docker-compose.yml --env-file "$$tmp" config -q
	cd $(SCRIPTS_DIR) && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q
	cd $(SCRIPTS_DIR) && bash -n *.sh && uvx --from shellcheck-py shellcheck *.sh
	cd $(PORTAL_DIR)/backend && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q
	cd $(PORTAL_DIR)/dev && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q
	cd $(PORTAL_DIR)/frontend && npm ci && npm run check
