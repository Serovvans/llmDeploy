#!/usr/bin/env bash
# Запуск команды с переменными из deploy/.env (docs/design.md §6.1).
#
# Запуск на ВМ, от root (файл .env читается только root):
#   sudo bash scripts/with_env.sh uv run smoke_test.py --direct
#
# Нужен там, где скрипту требуются секреты из .env (VLLM_API_KEY, BIFROST_ADMIN_*):
# значения читаются внутри этого процесса и передаются потомку через окружение, поэтому
# не попадают в список процессов хоста. Рабочий каталог — scripts/ (там uv-проект).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly ENV_FILE="${SCRIPT_DIR}/../deploy/.env"

die() {
  echo "ОШИБКА: $1" >&2
  echo "что делать: $2" >&2
  exit 1
}

[[ $# -gt 0 ]] || die "не задана команда" "bash scripts/with_env.sh КОМАНДА [аргументы]"
[[ -f "$ENV_FILE" ]] || die "нет ${ENV_FILE}" "сначала развернуть модель: sudo make model"

# Только строки вида NAME=значение: комментарии и пустые строки не попадают в окружение.
set -a
# shellcheck disable=SC1090  # источник формируется на лету из .env
source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")
set +a

cd "$SCRIPT_DIR"
exec "$@"
