# Однострочные команды развёртывания и эксплуатации LLM-сервиса (docs/design.md §9).
# Обёртки над scripts/ и docker compose: своей логики здесь нет.
#
# Развёртывание идёт этапами (docs/design.md §3.7, §3.8):
#   sudo make host && sudo reboot                          подготовка ВМ
#   make model                                             этап 1: модель (vLLM)
#   make cert                                              сертификат Let's Encrypt (для TLS_MODE=corp)
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
# Стенд без GPU (portal/dev) — отдельный проект compose на машине разработчика, без sudo;
# скрипт закрепляет имя проекта, чтобы команды не ушли в рабочий стек.
DEV_STAND := bash $(SCRIPTS_DIR)/dev_stand.sh
E2E_DIR := $(PORTAL_DIR)/frontend/e2e
INSTALL := $(SUDO) bash $(SCRIPTS_DIR)/install.sh
# Запускает команду в scripts/ с переменными из deploy/.env; секреты не идут в argv.
WITH_ENV := $(SUDO) bash $(SCRIPTS_DIR)/with_env.sh

# Параметры целей: make gateway LLM_HOSTNAME=llm.corp.example TLS_MODE=corp
PROFILES ?=
LLM_HOSTNAME ?=
TLS_MODE ?=
KEY ?=
# PEM корневого CA для смоук-тестов. Без него при TLS_MODE=internal smoke_test.py сам
# берёт deploy/caddy-root.crt; публичному сертификату (Let's Encrypt) CA не нужен.
CA ?=
NAME ?=
ID ?=
REQUESTS ?=
TOKENS ?=
SERVICE ?=
CONCURRENCY ?=
# Дополнительные аргументы smoke_test.py, например ARGS="--check-rate-limit 5".
ARGS ?=
# Адрес для смоук-тестов, если сервис не на 443: LLM_BASE_URL=https://llm.<домен>:18443/v1.
LLM_BASE_URL ?=
# Сертификат: MANUAL=1 — ручное добавление TXT-записи, если зона не на DNS Timeweb Cloud.
MANUAL ?=
# Портал: логин пользователя и отбор журнала аудита (make portal-audit SINCE=2026-10-01).
LOGIN ?=
SINCE ?=
EVENT ?=
LIMIT ?=
ONLY_ERRORS ?=
# Резервная копия: куда писать (make backup DIR=...) и откуда восстанавливать (FROM=...).
DIR ?=
FROM ?=
# Сквозные тесты: один браузер из playwright.config.ts (make dev-test PROJECT=chromium).
PROJECT ?=
# Эти значения попадают в команды только через окружение ("$$NAME"), а не подстановкой
# в текст рецепта: апостроф или пробел в ФИО и пути не ломают команду.
export LOGIN NAME SINCE EVENT LIMIT DIR FROM

.PHONY: help host preflight model cert cert-renew gateway smoke-model smoke bench key keys revoke \
        portal smoke-portal portal-admin portal-reset-2fa portal-unlock-login portal-audit \
        portal-reindex \
        portal-reindex-recreate portal-eval-search backup restore \
        up down restart ps logs preload \
        dev-up dev-reset dev-down dev-test-install dev-test check

help: ## Показать список целей
	@echo "Развёртывание LLM-сервиса. Использование: make <цель> [ПАРАМЕТР=значение]"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN { FS = ":.*?## " } { printf "  %-24s %s\n", $$1, $$2 }'
	@echo
	@echo "Этап 1 (модель):  make preflight && make model [PROFILES=monitoring]"
	@echo "Этап 2 (доступ):  make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp|internal (для corp с Let's Encrypt сначала make cert)"
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

# LLM_HOSTNAME, ACME_EMAIL и TIMEWEBCLOUD_AUTH_TOKEN читаются из deploy/.env; дальше
# сертификат продлевает таймер llm-cert-renew (его ставит make gateway).
cert: ## Этап 2: выпустить сертификат Let's Encrypt для LLM_HOSTNAME по DNS-01 ([MANUAL=1] — TXT-запись вручную)
	$(SUDO) bash $(SCRIPTS_DIR)/cert.sh issue $(if $(MANUAL),--manual)

cert-renew: ## Продлить сертификат, если до конца срока меньше 30 дней; Caddy перечитывает его без перезапуска ([MANUAL=1])
	$(SUDO) bash $(SCRIPTS_DIR)/cert.sh renew $(if $(MANUAL),--manual)

gateway: ## Этап 2: добавить Caddy и Bifrost (LLM_HOSTNAME=... TLS_MODE=corp|internal)
	$(INSTALL) --stage gateway --skip-preflight \
		$(if $(LLM_HOSTNAME),--hostname $(LLM_HOSTNAME)) \
		$(if $(TLS_MODE),--tls $(TLS_MODE)) \
		$(if $(PROFILES),--profiles $(PROFILES))

smoke: ## Этап 2: смоук-тест через HTTPS (KEY=sk-bf-... [CA=<PEM корневого CA>] [LLM_BASE_URL=https://<имя>:18443/v1])
	@[[ -n "$(KEY)" ]] || { echo "укажите KEY=sk-bf-... (создать: make key NAME=smoke)" >&2; exit 1; }
	$(WITH_ENV) env LLM_API_KEY='$(KEY)' $(if $(CA),LLM_CA_CERT='$(CA)') \
		$(if $(LLM_BASE_URL),LLM_BASE_URL='$(LLM_BASE_URL)') uv run smoke_test.py $(ARGS)

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

smoke-portal: ## Этап 3: что отдаёт сайт через 443 и здоровы ли сервисы; без запросов к модели ([CA=...] [LLM_BASE_URL=...])
	$(WITH_ENV) env $(if $(CA),LLM_CA_CERT='$(CA)') $(if $(LLM_BASE_URL),LLM_BASE_URL='$(LLM_BASE_URL)') uv run smoke_test.py --site-only --compose-file $(COMPOSE_FILE)

# Временный пароль печатается один раз и только на экран: команда не выводится (@) и
# ничего не пишет в файлы.
portal-admin: ## Создать администратора портала (LOGIN=ivanov NAME="Иванов И. И."); пароль выводится один раз
	@[[ -n "$$LOGIN" && -n "$$NAME" ]] || { echo 'укажите LOGIN=<логин> NAME="<ФИО>"' >&2; exit 1; }
	@$(COMPOSE) exec portal-api portal create-admin --login "$$LOGIN" --full-name "$$NAME"

portal-reset-2fa: ## Сбросить второй фактор пользователя портала (LOGIN=ivanov)
	@[[ -n "$$LOGIN" ]] || { echo "укажите LOGIN=<логин>" >&2; exit 1; }
	$(COMPOSE) exec portal-api portal reset-second-factor --login "$$LOGIN"

portal-unlock-login: ## Снять временную блокировку входа пользователя портала (LOGIN=ivanov)
	@[[ -n "$$LOGIN" ]] || { echo "укажите LOGIN=<логин>" >&2; exit 1; }
	$(COMPOSE) exec portal-api portal unlock-login --login "$$LOGIN"

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

# Запуск — тем же скриптом, что у юнита, и до systemctl: его вывод видит оператор. Пока
# есть следы прерванного восстановления (docs/portal-design.md §8), скрипт завершается с
# кодом 3, не запуская пишущие сервисы, и до systemctl дело не доходит.
up: ## Запустить стек и дождаться healthy (после прерванного make restore — без пишущих сервисов, с ошибкой)
	$(SUDO) bash $(SCRIPTS_DIR)/stack_up.sh --wait
	$(SUDO) systemctl start llm-stack || { echo "стек запущен, но юнит llm-stack не стартовал или не установлен (автозапуск после перезагрузки ставит make model)" >&2; exit 1; }

# compose down отдельно: у юнита в состоянии failed systemctl stop не выполняет ExecStop.
down: ## Остановить стек (systemd-юнит вместе с ним)
	$(SUDO) systemctl stop llm-stack
	$(COMPOSE) down

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

# Стенд портала с заглушкой модели: https://localhost:8443 (docs/portal-design.md §9).
# Секреты стенда — в portal/dev/.env (образец — portal/dev/.env.example). Цели dev-*
# работают только с проектом compose стенда; рабочий стек (deploy/) они не трогают.

dev-up: ## Стенд без GPU: собрать образы из текущего дерева, поднять, дождаться healthy
	$(DEV_STAND) up

dev-reset: ## Стенд: пересоздать с чистой базой (тома данных удаляются, caddy-data остаётся)
	$(DEV_STAND) reset

dev-down: ## Стенд: остановить и удалить контейнеры (данные в томах сохраняются)
	$(DEV_STAND) down

# Браузеры Playwright — сотни мегабайт: ставятся этой целью, а не при каждом прогоне.
dev-test-install: ## Сквозные тесты: один раз установить зависимости и браузеры Playwright
	cd $(E2E_DIR) && npm ci && npm run browsers

# Не запускать одновременно с make check: он переустанавливает зависимости тестов
# (npm ci в portal/frontend/e2e удаляет node_modules на время установки).
dev-test: ## Сквозные тесты на поднятом стенде ([PROJECT=chromium|firefox|webkit|chrome]); не одновременно с make check
	@[[ -d $(E2E_DIR)/node_modules ]] || { echo "нет зависимостей сквозных тестов: сначала make dev-test-install" >&2; exit 1; }
	cd $(E2E_DIR) && npm test $(if $(PROJECT),-- --project=$(PROJECT))

# Запускать без sudo: тесты бэкенда портала поднимают PostgreSQL, а он от root не стартует.
# Первому запуску нужна сеть (uv sync, npm ci).
check: ## Локальные проверки: docker compose config, ruff, mypy, pytest, shellcheck, портал; не одновременно с make dev-test
	@tmp=$$(mktemp); trap 'rm -f "$$tmp"' EXIT; \
		sed -e 's/^\([A-Za-z_]*\)=$$/\1=placeholder/' $(DEPLOY_DIR)/.env.example >"$$tmp"; \
		for profiles in "" "gateway" "gateway,embeddings,monitoring" \
				"gateway,embeddings,portal" "gateway,embeddings,monitoring,portal" \
				"gateway,embeddings,monitoring,portal,cert"; do \
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
	cd $(E2E_DIR) && npm ci && npm run typecheck
