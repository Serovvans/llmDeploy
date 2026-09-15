#!/usr/bin/env bash
# Бенчмарк модели: обёртка над `vllm bench serve` (docs/design.md §9, §12 этап 5).
#
# Запуск на ВМ, из любого каталога:
#   bash scripts/bench.sh                                # 8 параллельных запросов
#   bash scripts/bench.sh 16                             # другая параллельность
#   bash scripts/bench.sh 8 --random-input-len 8000      # доп. аргументы vllm bench serve
#
# Бенчмарк выполняется внутри запущенного контейнера vllm (docker compose exec) против его
# OpenAI-совместимого /v1 на 127.0.0.1:8000: измеряется сама модель, без TLS и лимитов
# ключей Bifrost. Дополнительные аргументы добавляются в конец и переопределяют умолчания.
# Вывод (TTFT, TPOT, ITL, throughput) печатается и сохраняется в scripts/bench-results/.
#
# Окружение (если не задано — берётся из deploy/.env):
#   MODEL_ID       HF-идентификатор модели, нужен для токенайзера (например Qwen/Qwen3.8-27B-FP8)
#   VLLM_API_KEY   внутренний ключ vLLM (--api-key)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly COMPOSE_FILE="${SCRIPT_DIR}/../deploy/docker-compose.yml"
readonly ENV_FILE="${SCRIPT_DIR}/../deploy/.env"
readonly RESULTS_DIR="${SCRIPT_DIR}/bench-results"
readonly SERVICE="vllm"
readonly VLLM_URL="http://127.0.0.1:8000"
readonly SERVED_MODEL="default"
readonly DEFAULT_CONCURRENCY=8
readonly PROMPTS_PER_SLOT=10
readonly INPUT_LEN=1024
readonly OUTPUT_LEN=512

die() {
  echo "ОШИБКА: $1" >&2
  echo "что делать: $2" >&2
  exit 1
}

# Значение переменной из окружения, иначе последняя строка NAME=... из deploy/.env.
env_value() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
  elif [[ -f "$ENV_FILE" ]]; then
    sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^[\"']//" -e "s/[\"']\$//"
  fi
}

main() {
  local concurrency="${1:-$DEFAULT_CONCURRENCY}"
  if [[ $# -gt 0 ]]; then shift; fi
  [[ "$concurrency" =~ ^[1-9][0-9]*$ ]] ||
    die "параллельность должна быть целым числом >= 1, получено «${concurrency}»" \
      "bash scripts/bench.sh [параллельность] [аргументы vllm bench serve]"

  command -v docker >/dev/null 2>&1 || die "docker не найден" "запустить bash scripts/preflight.sh"
  [[ -f "$COMPOSE_FILE" ]] || die "нет ${COMPOSE_FILE}" "запускать из клона репозитория на ВМ"

  local compose=(docker compose -f "$COMPOSE_FILE")
  local running
  running="$("${compose[@]}" ps --status running --services || true)"
  grep -qx "$SERVICE" <<<"$running" ||
    die "сервис ${SERVICE} не запущен" "docker compose -f deploy/docker-compose.yml up -d и дождаться healthy"

  local model_id vllm_api_key
  model_id="$(env_value MODEL_ID)"
  vllm_api_key="$(env_value VLLM_API_KEY)"
  [[ -n "$model_id" ]] || die "MODEL_ID не задан" "задать MODEL_ID в окружении или deploy/.env"
  [[ -n "$vllm_api_key" ]] || die "VLLM_API_KEY не задан" "задать VLLM_API_KEY в окружении или deploy/.env"

  local bench_args=(
    bench serve
    --backend openai-chat
    --base-url "$VLLM_URL"
    --endpoint /v1/chat/completions
    --model "$model_id"
    --served-model-name "$SERVED_MODEL"
    --dataset-name random
    --random-input-len "$INPUT_LEN"
    --random-output-len "$OUTPUT_LEN"
    --ignore-eos
    --num-prompts "$((concurrency * PROMPTS_PER_SLOT))"
    --max-concurrency "$concurrency"
    --percentile-metrics "ttft,tpot,itl,e2el"
    --metric-percentiles "50,90,99"
    "$@"
  )

  mkdir -p "$RESULTS_DIR"
  local result_file
  result_file="${RESULTS_DIR}/bench-c${concurrency}-$(date +%Y%m%d-%H%M%S).txt"
  {
    echo "# $(date -Iseconds) model=${model_id} concurrency=${concurrency}"
    echo "# vllm ${bench_args[*]}"
  } >"$result_file"

  # Ключ передаётся через окружение, а не в командной строке (не виден в ps).
  export OPENAI_API_KEY="$vllm_api_key"
  if ! "${compose[@]}" exec -T -e OPENAI_API_KEY "$SERVICE" vllm "${bench_args[@]}" 2>&1 |
    tee -a "$result_file"; then
    die "vllm bench serve завершился с ошибкой (лог: ${result_file})" \
      "проверить docker compose -f deploy/docker-compose.yml logs ${SERVICE}"
  fi
  echo "Результат сохранён: ${result_file}"
}

main "$@"
