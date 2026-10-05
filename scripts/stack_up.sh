#!/usr/bin/env bash
# Запуск стека: юнит llm-stack (ExecStart, рабочий каталог deploy/), make up, install.sh.
#
#   sudo bash scripts/stack_up.sh [аргументы docker compose up, например --wait]
#
# Не прямой `docker compose up -d`: пока есть следы прерванного восстановления, сервисы,
# пишущие в данные, запускать нельзя (docs/portal-design.md §8). Проверка, список этих
# сервисов и сам запуск — в scripts/backup.sh start; здесь только постоянная точка входа.
# Коды возврата: 0 — стек запущен; 3 — следы есть, пишущие сервисы остановлены, остальное
# запущено; 1 — следы проверить не удалось, пишущие сервисы не запущены.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR

exec bash "${SCRIPT_DIR}/backup.sh" start "$@"
