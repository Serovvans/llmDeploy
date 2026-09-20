#!/usr/bin/env bash
# Установка и запуск LLM-сервиса на подготовленной ВМ (docs/design.md §9, docs/runbook.md §2).
#
# Развёртывание идёт двумя этапами (docs/design.md §3.7); обёртки — в Makefile (make help).
# Запуск из клона репозитория на ВМ, после scripts/install_host.sh и перезагрузки:
#
#   sudo bash scripts/install.sh --stage model
#       Этап 1 — только модель: vLLM (+ выбранные профили). DNS-имя и сертификат не
#       нужны; модель доступна на 127.0.0.1:8000 для смоук-теста и бенчмарка.
#
#   sudo bash scripts/install.sh --stage gateway --hostname llm.corp.example --tls corp
#       Этап 2 — внешний доступ: добавляет профиль gateway (Caddy + Bifrost) и 443.
#
# Аргументы (при повторном запуске берутся из deploy/.env, а заданные явно —
# перезаписывают значение в .env):
#   --stage model|gateway  этап; по умолчанию — тот, что уже записан в deploy/.env.
#                          gateway только добавляет профиль: чтобы вернуться к одной
#                          модели, уберите gateway из COMPOSE_PROFILES в deploy/.env
#   --hostname NAME    DNS-имя сервиса (LLM_HOSTNAME); нужно на этапе gateway
#   --tls MODE         corp — сертификат в deploy/certs/{fullchain,privkey}.pem;
#                      internal — собственный CA Caddy (TLS_MODE); нужно на этапе gateway
#   --profiles LIST    дополнительные профили: "", embeddings, monitoring или
#                      embeddings,monitoring (COMPOSE_PROFILES)
#   --skip-preflight   не запускать scripts/preflight.sh (например, при повторном запуске)
#
# Что делает:
#   1. preflight.sh;
#   2. deploy/.env из .env.example; пустые секреты генерируются, заданные не меняются;
#   3. (gateway + corp) проверка сертификата: срок, имя хоста, соответствие ключу;
#   4. docker compose config и pull;
#   5. предзагрузка весов моделей в volume hf-cache;
#   6. systemd-юнит llm-stack с путём к этому клону, запуск и ожидание healthy;
#   7. (gateway + internal) выгрузка корневого сертификата Caddy в deploy/caddy-root.crt;
#   8. проверка: этап model — на 127.0.0.1:8000 запрос без ключа даёт 401, с ключом
#      модель отвечает; этап gateway — через 443 запрос без ключа 401/403, / — 404.
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
# Профиль второго этапа: Caddy и Bifrost (docs/design.md §3.7).
readonly GATEWAY_PROFILE="gateway"
readonly VLLM_LOCAL_URL="http://127.0.0.1:8000"
readonly MINIMAL_CHAT_BODY='{"model": "default", "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1}'
# Первый запрос к прогретой модели укладывается в секунды; запас — на загруженную ВМ.
readonly HTTP_TIMEOUT_S=60

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
  arg_stage=""
  arg_hostname=""
  arg_tls=""
  arg_profiles=""
  profiles_given=0
  skip_preflight=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --stage | --hostname | --tls | --profiles)
        [[ $# -ge 2 ]] || die "$1 требует значение" "bash scripts/install.sh --help"
        case "$1" in
          --stage) arg_stage="$2" ;;
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
  [[ -z "$arg_stage" || "$arg_stage" == "model" || "$arg_stage" == "gateway" ]] ||
    die "--stage «${arg_stage}»: ожидается model или gateway" \
      "--stage model — только модель, --stage gateway — добавить внешний доступ (docs/design.md §3.7)"
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

# Убирает профиль gateway из списка, оставляя дополнительные (embeddings, monitoring).
without_gateway() {
  local list=",$1,"
  list="${list//,${GATEWAY_PROFILE},/,}"
  list="${list#,}"
  printf '%s' "${list%,}"
}

# Определяет этап и итоговый COMPOSE_PROFILES: дополнительные профили из --profiles
# (иначе сохраняются из .env) плюс gateway, если внешний доступ уже включён или его
# включает --stage gateway. Этап только добавляет: обратно — правкой .env вручную.
resolve_stage() {
  local current extras
  current="$(env_file_value COMPOSE_PROFILES)"

  gateway_enabled=0
  [[ ",${current}," == *",${GATEWAY_PROFILE},"* ]] && gateway_enabled=1
  [[ "$arg_stage" == "gateway" ]] && gateway_enabled=1

  if [[ "$profiles_given" -eq 1 ]]; then
    extras="$arg_profiles"
  else
    extras="$(without_gateway "$current")"
  fi
  [[ "$extras" =~ $PROFILES_RE ]] ||
    die "--profiles «${extras}»: допустимы пусто, embeddings, monitoring, embeddings,monitoring" \
      "внешний доступ включается не здесь, а через --stage gateway"

  profiles="$extras"
  [[ "$gateway_enabled" -eq 0 ]] || profiles="${GATEWAY_PROFILE}${extras:+,${extras}}"
  if [[ "$gateway_enabled" -eq 1 ]]; then
    log "этап gateway: модель + внешний доступ (Caddy, Bifrost)"
  else
    log "этап model: только модель на ${VLLM_LOCAL_URL}, без внешнего доступа"
  fi
}

prepare_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    install -m 600 "$ENV_EXAMPLE" "$ENV_FILE"
    log "создан ${ENV_FILE} из .env.example"
  fi
  chmod 600 "$ENV_FILE"

  resolve_stage

  # Аргументы важнее .env; в файл пишем только после проверки.
  hostname="${arg_hostname:-$(env_file_value LLM_HOSTNAME)}"
  tls_mode="${arg_tls:-$(env_file_value TLS_MODE)}"

  # DNS-имя и режим TLS нужны только Caddy, то есть на этапе gateway.
  if [[ "$gateway_enabled" -eq 1 ]]; then
    [[ "$hostname" =~ $HOSTNAME_RE ]] ||
      die "LLM_HOSTNAME «${hostname}» не задан или не похож на DNS-имя" "указать --hostname llm.<корп.домен>"
    [[ "$tls_mode" == "corp" || "$tls_mode" == "internal" ]] ||
      die "TLS_MODE «${tls_mode}»: ожидается corp или internal" \
        "указать --tls corp (есть сертификат корпоративного CA) или --tls internal (docs/design.md §3.4)"
  fi
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
  [[ "$gateway_enabled" -eq 1 ]] || return 0
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
  [[ "$gateway_enabled" -eq 1 && "$tls_mode" == "internal" ]] || return 0
  compose cp "caddy:${CADDY_ROOT_CERT_PATH}" "$ROOT_CERT_FILE" ||
    die "не удалось выгрузить корневой сертификат Caddy" "cd ${DEPLOY_DIR} && docker compose logs caddy"
  chmod 644 "$ROOT_CERT_FILE"
  log "корневой сертификат Caddy: ${ROOT_CERT_FILE}"
}

# expect_http "описание" "ожидаемые коды через |" URL [доп. аргументы curl] "подсказка"
# Дополнительные параметры curl читаются со stdin (--config -): так туда попадает
# заголовок с ключом, не видный в списке процессов хоста.
expect_http() {
  local label="$1" expected="$2" url="$3" hint="$4"
  shift 4
  local code
  code=$(curl -sS -o /dev/null --max-time "$HTTP_TIMEOUT_S" -w '%{http_code}' --config - "$@" "$url") ||
    die "${label}: запрос к ${url} не выполнен" "$hint"
  [[ "$code" =~ ^(${expected})$ ]] ||
    die "${label}: HTTP ${code}, ожидалось ${expected}" "$hint"
  log "проверка: ${label} — HTTP ${code}"
}

# Этап model: модель отвечает на 127.0.0.1:8000 и требует внутренний ключ.
verify_model() {
  local url="${VLLM_LOCAL_URL}/v1/chat/completions"
  local hint="cd ${DEPLOY_DIR} && docker compose logs vllm; docs/runbook.md §5"
  local post=(-X POST -H "Content-Type: application/json" -d "$MINIMAL_CHAT_BODY")

  expect_http "запрос к vLLM без ключа отклоняется" "401" "$url" "$hint" "${post[@]}" </dev/null
  expect_http "модель отвечает на ${VLLM_LOCAL_URL}" "200" "$url" "$hint" "${post[@]}" \
    < <(printf 'header = "Authorization: Bearer %s"\n' "$(env_file_value VLLM_API_KEY)")
}

# Этап gateway: наружу через 443 видны только /v1/* и только с ключом Bifrost.
verify_gateway() {
  local hint="docs/runbook.md §5; cd ${DEPLOY_DIR} && docker compose logs caddy bifrost"
  local tls_args=(--cacert "$ROOT_CERT_FILE")
  # Корень корпоративного CA на ВМ может быть не установлен; сертификат уже проверен выше.
  [[ "$tls_mode" == "internal" ]] || tls_args=(--insecure)
  local common=("${tls_args[@]}" --resolve "${hostname}:443:127.0.0.1")

  # Именно inference-запрос: на него действует enforce_auth_on_inference Bifrost.
  expect_http "запрос к модели без ключа отклоняется" "401|403" \
    "https://${hostname}/v1/chat/completions" "$hint" "${common[@]}" \
    -X POST -H "Content-Type: application/json" -d "$MINIMAL_CHAT_BODY" </dev/null
  expect_http "/ через 443 закрыт" "404" "https://${hostname}/" "$hint" "${common[@]}" </dev/null
}

verify_endpoint() {
  if [[ "$gateway_enabled" -eq 1 ]]; then
    verify_gateway
  else
    verify_model
  fi
}

print_model_summary() {
  cat <<EOF

===== Этап 1 завершён: модель развёрнута =====
Модель:    ${VLLM_LOCAL_URL}/v1   model="default"   ключ — VLLM_API_KEY
Секреты:   ${ENV_FILE} (root, 600)

Порт открыт только на 127.0.0.1; с рабочей станции — через SSH-туннель:
  ssh -L 8000:127.0.0.1:8000 <админ>@<вм>

Дальше (docs/runbook.md §2.5, §2.6):
  1. Смоук-тест модели:  make smoke-model
  2. Бенчмарк:           make bench
  3. Когда DevOps выдадут DNS-имя и сертификат — этап 2:
       make gateway LLM_HOSTNAME=llm.<корп.домен> TLS_MODE=corp
EOF
}

print_gateway_summary() {
  cat <<EOF

===== Этап 2 завершён: внешний доступ открыт =====
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

Дальше (docs/runbook.md §2.5):
  1. Ключ и смоук-тест через 443:
       make key NAME=smoke
       make smoke KEY=sk-bf-... CA=${ca_cert}
  2. UI Bifrost и Grafana с рабочей станции: ssh -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 <админ>@<вм>
EOF
}

print_summary() {
  if [[ "$gateway_enabled" -eq 1 ]]; then
    print_gateway_summary
  else
    print_model_summary
  fi
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
