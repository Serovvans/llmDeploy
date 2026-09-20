# Однострочные команды развёртывания и эксплуатации LLM-сервиса (docs/design.md §9).
# Обёртки над scripts/ и docker compose: своей логики здесь нет.
#
# Развёртывание идёт двумя этапами (docs/design.md §3.7):
#   sudo make host && sudo reboot                          подготовка ВМ
#   make model                                             этап 1: модель (vLLM)
#   make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp    этап 2: внешний доступ
#
# `make help` — список целей. Цели работают с docker, systemd и deploy/.env, поэтому
# сами подставляют sudo, если Makefile запущен не от root.

SHELL := /bin/bash
.DEFAULT_GOAL := help

ROOT_DIR := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
DEPLOY_DIR := $(ROOT_DIR)/deploy
SCRIPTS_DIR := $(ROOT_DIR)/scripts
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

.PHONY: help host preflight model gateway smoke-model smoke bench key keys revoke \
        up down restart ps logs preload check

help: ## Показать список целей
	@echo "Развёртывание LLM-сервиса. Использование: make <цель> [ПАРАМЕТР=значение]"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN { FS = ":.*?## " } { printf "  %-14s %s\n", $$1, $$2 }'
	@echo
	@echo "Этап 1 (модель):  make preflight && make model [PROFILES=monitoring]"
	@echo "Этап 2 (доступ):  make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp|internal"

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

# --- эксплуатация ---

up: ## Запустить стек и дождаться healthy
	$(SUDO) systemctl start llm-stack
	$(COMPOSE) up -d --wait

down: ## Остановить стек (systemd-юнит вместе с ним)
	$(SUDO) systemctl stop llm-stack

restart: ## Перезапустить стек
	$(SUDO) systemctl restart llm-stack

ps: ## Состояние контейнеров
	$(COMPOSE) ps

logs: ## Логи (SERVICE=vllm|bifrost|caddy; по умолчанию все)
	$(COMPOSE) logs -f --tail=200 $(SERVICE)

preload: ## Докачать веса MODEL_ID в volume hf-cache (после смены модели)
	$(WITH_ENV) bash -c 'exec docker compose -f $(COMPOSE_FILE) run --rm --no-deps \
		-e HF_HUB_OFFLINE=0 --entrypoint hf vllm download "$$MODEL_ID"'

# --- разработка (локально, без GPU) ---

check: ## Локальные проверки: docker compose config, ruff, mypy, pytest, shellcheck
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
		sed -e 's/^\([A-Za-z_]*\)=$$/\1=placeholder/' $(DEPLOY_DIR)/.env.example >"$$tmp"; \
		for profiles in "" "gateway" "gateway,embeddings,monitoring"; do \
			echo "==> docker compose config COMPOSE_PROFILES=$$profiles"; \
			COMPOSE_PROFILES="$$profiles" docker compose -f $(COMPOSE_FILE) --env-file "$$tmp" config -q || exit 1; \
		done
	cd $(SCRIPTS_DIR) && uv run ruff format --check . && uv run ruff check . && uv run mypy && uv run pytest -q
	cd $(SCRIPTS_DIR) && bash -n *.sh && uvx --from shellcheck-py shellcheck *.sh
