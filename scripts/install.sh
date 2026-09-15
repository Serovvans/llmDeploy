#!/usr/bin/env bash
# Установка и запуск LLM-сервиса на подготовленной ВМ (docs/design.md §9, docs/runbook.md §2).
#
# Запуск из клона репозитория на ВМ, после scripts/install_host.sh и перезагрузки:
#   sudo bash scripts/install.sh --hostname llm.corp.example --tls corp
#   sudo bash scripts/install.sh --hostname llm.corp.example --tls internal --profiles monitoring
#
# Аргументы (нужны при первом запуске; при повторном берутся из deploy/.env, а заданные
# явно — перезаписывают значение в .env):
#   --hostname NAME    DNS-имя сервиса (LLM_HOSTNAME)
#   --tls MODE         corp — сертификат в deploy/certs/{fullchain,privkey}.pem;
#                      internal — собственный CA Caddy (TLS_MODE)
#   --profiles LIST    "", embeddings, monitoring или embeddings,monitoring (COMPOSE_PROFILES)
#   --skip-preflight   не запускать scripts/preflight.sh (например, при повторном запуске)
#
# Что делает:
#   1. preflight.sh;
#   2. deploy/.env из .env.example; пустые секреты генерируются, заданные не меняются;
#   3. проверка сертификата (corp): срок, имя хоста, соответствие ключу;
#   4. docker compose config и pull;
#   5. предзагрузка весов моделей в volume hf-cache;
#   6. systemd-юнит llm-stack с путём к этому клону, запуск и ожидание healthy;
#   7. (internal) выгрузка корневого сертификата Caddy в deploy/caddy-root.crt;
#   8. проверка через 443: запрос к модели без ключа — 401/403, / — 404.
# Идемпотентен: повторный запуск применяет изменения .env и compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../deploy" && pwd)"
readonly DEPLOY_DIR
readonly ENV_FILE="${DEPLOY_DIR}/.env"
readonly ENV_EXAMPLE="${DEPLOY_DIR}/.env.example"
readonly CERTS_DIR="${DEPLOY_DIR}/certs"
readonly ROOT_CERT_FILE="${DEPLOY_DIR}/caddy-root.crt"
readonly UNIT_SOURCE="${DEPLOY_DIR}/systemd/llm-stack.service"
readonly UNIT_TARGET="/etc/systemd/system/llm-stack.service"
readonly CADDY_ROOT_CERT_PATH="/data/caddy/pki/authorities/local/root.crt"
readonly DEFAULT_ADMIN_USERNAME="admin"
readonly CERT_WARN_DAYS=30
readonly HOSTNAME_RE='^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'
readonly PROFILES_RE='^((embeddings|monitoring)(,(embeddings|monitoring))?)?$'

log() {
  echo "==> $1"
}

die() {
  echo "ОШИБКА: $1" >&2
  echo "что делать: $2" >&2
  exit 1
}

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# Значение NAME из deploy/.env (последняя строка NAME=...), без окружающих кавычек.
env_file_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

# Заменяет строку NAME=... в deploy/.env или дописывает её; права 600 сохраняются.
set_env_value() {
  local tmp
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  NAME="$1" VALUE="$2" awk '
    index($0, ENVIRON["NAME"] "=") == 1 {
      if (!done) print ENVIRON["NAME"] "=" ENVIRON["VALUE"]
      done = 1
      next
    }
    { print }
    END { if (!done) print ENVIRON["NAME"] "=" ENVIRON["VALUE"] }
  ' "$ENV_FILE" >"$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# Генерирует значение, только если в .env оно пустое: секреты не перезаписываются.
ensure_secret() {
  local name="$1" generator="$2"
  if [[ -z "$(env_file_value "$name")" ]]; then
    set_env_value "$name" "$($generator)"
    log "сгенерирован ${name}"
  fi
}

random_hex() {
  openssl rand -hex 32
}

random_base64() {
  openssl rand -base64 32
}

# -f, а не --project-directory: файл compose ищется по пути, .env и относительные пути
# (./certs, ./caddy) берутся из его каталога — так же, как в systemd-юните.
compose() {
  docker compose -f "${DEPLOY_DIR}/docker-compose.yml" "$@"
}

parse_args() {
  arg_hostname=""
  arg_tls=""
  arg_profiles=""
  profiles_given=0
  skip_preflight=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --hostname | --tls | --profiles)
        [[ $# -ge 2 ]] || die "$1 требует значение" "bash scripts/install.sh --help"
        case "$1" in
          --hostname) arg_hostname="$2" ;;
          --tls) arg_tls="$2" ;;
          --profiles) arg_profiles="$2"; profiles_given=1 ;;
        esac
        shift 2
        ;;
      --skip-preflight) skip_preflight=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "неизвестный аргумент «$1»" "bash scripts/install.sh --help" ;;
    esac
  done
}

check_system() {
  [[ "$(uname -s)" == "Linux" ]] || die "скрипт запускается на ВМ с Ubuntu" "запустить на ВМ"
  [[ "$EUID" -eq 0 ]] || die "нужны права root (docker, systemd)" "sudo bash scripts/install.sh ..."
  local cmd
  for cmd in docker openssl curl systemctl; do
    command -v "$cmd" >/dev/null 2>&1 || die "не найден ${cmd}" "sudo bash scripts/install_host.sh"
  done
}

run_preflight() {
  if [[ "$skip_preflight" -eq 1 ]]; then
    log "preflight пропущен (--skip-preflight)"
    return
  fi
  log "preflight"
  bash "${SCRIPT_DIR}/preflight.sh" ||
    die "preflight не прошёл" "исправить пункты FAIL из сводки выше и повторить установку"
}

prepare_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    install -m 600 "$ENV_EXAMPLE" "$ENV_FILE"
    log "создан ${ENV_FILE} из .env.example"
  fi
  chmod 600 "$ENV_FILE"

  # Аргументы важнее .env; в файл пишем только после проверки.
  hostname="${arg_hostname:-$(env_file_value LLM_HOSTNAME)}"
  tls_mode="${arg_tls:-$(env_file_value TLS_MODE)}"
  profiles="$(env_file_value COMPOSE_PROFILES)"
  [[ "$profiles_given" -eq 0 ]] || profiles="$arg_profiles"

  [[ "$hostname" =~ $HOSTNAME_RE ]] ||
    die "LLM_HOSTNAME «${hostname}» не задан или не похож на DNS-имя" "указать --hostname llm.<корп.домен>"
  [[ "$tls_mode" == "corp" || "$tls_mode" == "internal" ]] ||
    die "TLS_MODE «${tls_mode}»: ожидается corp или internal" \
      "указать --tls corp (есть сертификат корпоративного CA) или --tls internal (docs/design.md §3.4)"
  [[ "$profiles" =~ $PROFILES_RE ]] ||
    die "COMPOSE_PROFILES «${profiles}»: допустимы пусто, embeddings, monitoring, embeddings,monitoring" \
      "указать --profiles monitoring (или другое допустимое значение)"
  set_env_value LLM_HOSTNAME "$hostname"
  set_env_value TLS_MODE "$tls_mode"
  set_env_value COMPOSE_PROFILES "$profiles"

  if [[ -z "$(env_file_value BIFROST_ADMIN_USERNAME)" ]]; then
    set_env_value BIFROST_ADMIN_USERNAME "$DEFAULT_ADMIN_USERNAME"
  fi
  ensure_secret VLLM_API_KEY random_hex
  ensure_secret BIFROST_ADMIN_PASSWORD random_hex
  ensure_secret BIFROST_ENCRYPTION_KEY random_base64
  ensure_secret GRAFANA_ADMIN_PASSWORD random_hex
}

check_corp_certificate() {
  local cert="${CERTS_DIR}/fullchain.pem" key="${CERTS_DIR}/privkey.pem"
  local fix="положить сертификат корпоративного CA в ${cert} и ключ в ${key}, либо установить с --tls internal"
  [[ -f "$cert" ]] || die "нет файла ${cert}" "$fix"
  [[ -f "$key" ]] || die "нет файла ${key}" "$fix"
  chmod 600 "$key"

  openssl x509 -in "$cert" -noout >/dev/null 2>&1 ||
    die "${cert} не является PEM-сертификатом" "$fix"
  openssl x509 -in "$cert" -noout -checkend 0 >/dev/null ||
    die "сертификат ${cert} просрочен" "запросить перевыпуск у DevOps (docs/devops-request.md §1.2)"
  if ! openssl x509 -in "$cert" -noout -checkend "$((CERT_WARN_DAYS * 86400))" >/dev/null; then
    log "ВНИМАНИЕ: сертификат истекает менее чем через ${CERT_WARN_DAYS} дней"
  fi
  [[ "$(openssl x509 -in "$cert" -noout -checkhost "$hostname")" == *"does match"* ]] ||
    die "сертификат ${cert} выписан не на ${hostname}" "запросить сертификат с SAN = ${hostname}"
  [[ "$(openssl x509 -in "$cert" -noout -pubkey)" == "$(openssl pkey -in "$key" -pubout 2>/dev/null)" ]] ||
    die "ключ ${key} не соответствует сертификату ${cert}" "проверить, что файлы из одной пары"
  log "сертификат корпоративного CA: срок, имя и ключ в порядке"
}

prepare_tls() {
  install -d -m 700 "$CERTS_DIR"
  if [[ "$tls_mode" == "corp" ]]; then
    check_corp_certificate
  else
    log "TLS: собственный CA Caddy (корень будет выгружен после запуска)"
  fi
}

pull_images() {
  log "проверка конфигурации compose"
  compose config -q
  log "загрузка образов"
  compose pull
}

preload_model() {
  local model_id="$1"
  log "предзагрузка весов ${model_id} (повторный запуск докачивает только недостающее)"
  compose run --rm --no-deps -e HF_HUB_OFFLINE=0 --entrypoint hf vllm download "$model_id"
}

preload_models() {
  preload_model "$(env_file_value MODEL_ID)"
  if [[ ",${profiles}," == *",embeddings,"* ]]; then
    preload_model "$(env_file_value EMBED_MODEL_ID)"
  fi
}

start_stack() {
  log "systemd-юнит llm-stack (WorkingDirectory=${DEPLOY_DIR})"
  sed "s#^WorkingDirectory=.*#WorkingDirectory=${DEPLOY_DIR}#" "$UNIT_SOURCE" >"$UNIT_TARGET"
  systemctl daemon-reload
  systemctl enable llm-stack

  log "запуск: vLLM загружает модель несколько минут; логи — cd ${DEPLOY_DIR} && docker compose logs -f vllm"
  # start — первый запуск через юнит; up --wait — применить изменения при повторной
  # установке (у активного oneshot-юнита start ничего не делает) и дождаться healthy.
  systemctl start llm-stack ||
    die "не удалось запустить стек" "sudo journalctl -u llm-stack; cd ${DEPLOY_DIR} && docker compose ps"
  compose up -d --wait ||
    die "не все сервисы стали healthy" "cd ${DEPLOY_DIR} && docker compose ps и logs <сервис>; docs/runbook.md §5"
}

export_root_certificate() {
  [[ "$tls_mode" == "internal" ]] || return 0
  compose cp "caddy:${CADDY_ROOT_CERT_PATH}" "$ROOT_CERT_FILE" ||
    die "не удалось выгрузить корневой сертификат Caddy" "cd ${DEPLOY_DIR} && docker compose logs caddy"
  chmod 644 "$ROOT_CERT_FILE"
  log "корневой сертификат Caddy: ${ROOT_CERT_FILE}"
}

# expect_http "описание" "ожидаемые коды через |" путь [доп. аргументы curl]
expect_http() {
  local label="$1" expected="$2" path="$3"
  shift 3
  local tls_args=(--cacert "$ROOT_CERT_FILE")
  # Корень корпоративного CA на ВМ может быть не установлен; сертификат уже проверен выше.
  [[ "$tls_mode" == "internal" ]] || tls_args=(--insecure)
  local code
  code=$(curl -sS -o /dev/null --max-time 30 -w '%{http_code}' "${tls_args[@]}" "$@" \
    --resolve "${hostname}:443:127.0.0.1" "https://${hostname}${path}") ||
    die "${label}: запрос к https://${hostname}${path} не выполнен" \
      "cd ${DEPLOY_DIR} && docker compose logs caddy"
  [[ "$code" =~ ^(${expected})$ ]] ||
    die "${label}: HTTP ${code}, ожидалось ${expected}" "docs/runbook.md §5; проверить deploy/bifrost/config.json и Caddyfile"
  log "проверка: ${label} — HTTP ${code}"
}

verify_endpoint() {
  # Именно inference-запрос: на него действует enforce_auth_on_inference Bifrost.
  expect_http "запрос к модели без ключа отклоняется" "401|403" "/v1/chat/completions" \
    -X POST -H "Content-Type: application/json" \
    -d '{"model": "default", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}'
  expect_http "/ через 443 закрыт" "404" "/"
}

print_summary() {
  cat <<EOF

===== Установка завершена =====
API для клиентов:  https://${hostname}/v1   model="default"
Секреты:           ${ENV_FILE} (root, 600)

ВАЖНО: сохраните копию BIFROST_ENCRYPTION_KEY из ${ENV_FILE} вне ВМ, в хранилище
секретов. Без неё базу ключей Bifrost из бэкапа не восстановить.
EOF
  if [[ "$tls_mode" == "internal" ]]; then
    cat <<EOF

TLS: собственный CA Caddy. Раздайте ${ROOT_CERT_FILE} серверам приложений и UI
и укажите его как доверенный CA в приложении (docs/runbook.md §3.2). Volume llm_caddy-data
с ключом корня — в бэкап (docs/runbook.md §4.4).
EOF
  fi
  local ca_cert="<PEM корпоративного корневого CA>"
  [[ "$tls_mode" == "corp" ]] || ca_cert="$ROOT_CERT_FILE"
  cat <<EOF

Дальше (docs/runbook.md §2.5–2.6):
  1. Ключ и смоук-тест (на ВМ, в sudo -i; BIFROST_ADMIN_* — из ${ENV_FILE}):
       cd ${SCRIPT_DIR} && uv run keys.py create --name smoke --requests 100
       LLM_HOSTNAME=${hostname} LLM_CA_CERT=${ca_cert} LLM_API_KEY=sk-bf-... uv run smoke_test.py
  2. Бенчмарк: sudo bash ${SCRIPT_DIR}/bench.sh
  3. UI Bifrost и Grafana с рабочей станции: ssh -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 <админ>@<вм>
EOF
}

main() {
  parse_args "$@"
  check_system
  run_preflight
  prepare_env
  prepare_tls
  pull_images
  preload_models
  start_stack
  export_root_certificate
  verify_endpoint
  print_summary
}

main "$@"
