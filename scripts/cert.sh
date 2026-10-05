#!/usr/bin/env bash
# Выпуск и продление сертификата Let's Encrypt для LLM_HOSTNAME (docs/portal-design.md
# §3.2). Запуск на ВМ из клона репозитория, от root; обёртки — make cert / cert-renew.
#
#   sudo bash scripts/cert.sh issue [--manual]
#       Первый выпуск. Если сертификат уже выпущен, действует как renew.
#
#   sudo bash scripts/cert.sh renew [--manual]
#       Продление, если до конца срока меньше 30 дней; иначе ничего не делает и
#       завершается с кодом 0. Так его запускает таймер llm-cert-renew.
#
# Внешние 80 и 443 не проброшены, поэтому владение доменом подтверждается только
# записью DNS (DNS-01). ACME-клиент — lego, сервис compose `lego` профиля cert; его
# состояние (ключ учётной записи ACME, выпущенные сертификаты) — в deploy/lego/.
#
# Из deploy/.env читаются:
#   LLM_HOSTNAME             доменное имя сервиса (не IP-адрес);
#   ACME_EMAIL               e-mail учётной записи Let's Encrypt;
#   TIMEWEBCLOUD_AUTH_TOKEN  токен API DNS Timeweb Cloud: lego сам создаёт и удаляет
#                            TXT-запись.
# Скрипт проверяет только, что e-mail и токен заданы: в контейнер их передаёт compose
# (LEGO_EMAIL, TIMEWEBCLOUD_AUTH_TOKEN), в аргументы и вывод токен не попадает.
#
# --manual — запасной путь, если зона не обслуживается DNS Timeweb Cloud: lego
# показывает TXT-запись, оператор добавляет её у своего провайдера DNS и нажимает Enter.
# Токен не нужен; таймер так продлевать не может — продление вручную раз в 60 дней.
#
# Порядок: lego выпускает или продлевает сертификат в deploy/lego/; сертификат и ключ
# проверяются (пара, имя хоста, срок); затем раскладываются в deploy/certs/
# (fullchain.pem — 644, privkey.pem — 600) заменой файла целиком; затем Caddy, если он
# запущен, перечитывает файлы без разрыва соединений. При любом отказе до раскладки
# работающий сертификат и Caddy не тронуты. Если Caddy перечитать файлы не смог, код
# возврата ненулевой, и следующий запуск (таймер) повторяет перечитывание.
# Терминал не нужен (кроме --manual).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../deploy" && pwd)"
readonly DEPLOY_DIR
readonly ENV_FILE="${DEPLOY_DIR}/.env"
readonly CERTS_DIR="${DEPLOY_DIR}/certs"
readonly CERT_FILE="${CERTS_DIR}/fullchain.pem"
readonly KEY_FILE="${CERTS_DIR}/privkey.pem"
# Отметка «файлы разложены, Caddy их ещё не перечитал»: ставится до замены файлов и
# снимается после перечитывания. Пока она есть, каждый запуск повторяет перечитывание.
readonly RELOAD_PENDING="${CERTS_DIR}/.reload-pending"
# Каталог состояния lego на хосте; путь внутри контейнера задаёт compose (LEGO_PATH).
readonly LEGO_DIR="${DEPLOY_DIR}/lego"
readonly LEGO_SERVICE="lego"
readonly LEGO_PROFILE="cert"
readonly DNS_PROVIDER="timewebcloud"
readonly DNS_TOKEN_VAR="TIMEWEBCLOUD_AUTH_TOKEN"
readonly RENEW_DAYS=30
readonly CADDYFILE_PATH="/etc/caddy/Caddyfile"
readonly HOSTNAME_RE='^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'
readonly IP_RE='^[0-9.]+$'

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

compose() {
  docker compose -f "${DEPLOY_DIR}/docker-compose.yml" "$@"
}

# Значение NAME из deploy/.env (последняя строка NAME=...), без окружающих кавычек.
env_file_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

load_settings() {
  [[ -r "$ENV_FILE" ]] ||
    die "нет ${ENV_FILE} или он не читается" "запускать от root (sudo make cert); .env создаёт этап 1 (make model)"
  hostname="$(env_file_value LLM_HOSTNAME)"
  [[ -n "$hostname" ]] ||
    die "LLM_HOSTNAME в ${ENV_FILE} пуст" "вписать LLM_HOSTNAME=llm.<домен> в ${ENV_FILE}"
  [[ ! "$hostname" =~ $IP_RE ]] ||
    die "LLM_HOSTNAME=${hostname} — IP-адрес: Let's Encrypt выдаёт сертификат только на доменное имя" \
      "вписать доменное имя в LLM_HOSTNAME; для доступа по IP сертификат не нужен (TLS_MODE=internal)"
  [[ "$hostname" =~ $HOSTNAME_RE ]] ||
    die "LLM_HOSTNAME «${hostname}» не похож на доменное имя" "вписать LLM_HOSTNAME=llm.<домен> в ${ENV_FILE}"
  [[ -n "$(env_file_value ACME_EMAIL)" ]] ||
    die "ACME_EMAIL в ${ENV_FILE} пуст" "вписать ACME_EMAIL=<e-mail для уведомлений Let's Encrypt> в ${ENV_FILE}"
  dns="$DNS_PROVIDER"
  if [[ "$manual" -eq 1 ]]; then
    dns="manual"
  elif [[ -z "$(env_file_value "$DNS_TOKEN_VAR")" ]]; then
    die "${DNS_TOKEN_VAR} в ${ENV_FILE} пуст" \
      "вписать токен API Timeweb Cloud; если зона домена не на DNS Timeweb Cloud — ручное подтверждение: MANUAL=1"
  fi
  lego_cert="${LEGO_DIR}/certificates/${hostname}.crt"
  lego_key="${LEGO_DIR}/certificates/${hostname}.key"
}

# У lego 5 одна команда на выпуск и продление: без сертификата в своём состоянии он
# выпускает новый, с сертификатом — продлевает, когда до конца срока меньше RENEW_DAYS.
request_certificate() {
  log "$1 сертификата Let's Encrypt для ${hostname} (DNS-01, ${dns})"
  # Ключ учётной записи ACME: каталог, созданный самим Docker, был бы доступен всем.
  install -d -m 700 "$LEGO_DIR"
  compose --profile "$LEGO_PROFILE" run --rm "$LEGO_SERVICE" run --domains "$hostname" \
    --dns "$dns" --accept-tos --renew-days "$RENEW_DAYS" ||
    die "lego завершился с ошибкой: сертификат не получен" \
      "причина — в выводе lego выше; проверить ${DNS_TOKEN_VAR} и что зона ${hostname} обслуживается DNS Timeweb Cloud (иначе MANUAL=1); работающий сертификат и Caddy не тронуты"
}

expires_soon() {
  ! openssl x509 -in "$1" -noout -checkend "$((RENEW_DAYS * 86400))" >/dev/null 2>&1
}

# В deploy/certs/ лежит ровно то, что выдал lego.
deployed() {
  cmp -s "$lego_cert" "$CERT_FILE" && cmp -s "$lego_key" "$KEY_FILE"
}

# Проверяется то, что выдал lego, до замены рабочих файлов.
verify_pair() {
  local cert="$1" key="$2"
  local hint="рабочие файлы в ${CERTS_DIR} не тронуты; посмотреть ${LEGO_DIR}/certificates и вывод lego"
  [[ -s "$cert" && -s "$key" ]] || die "lego не оставил ${cert} и ${key}" "$hint"
  openssl x509 -in "$cert" -noout >/dev/null 2>&1 ||
    die "${cert} не является PEM-сертификатом" "$hint"
  openssl x509 -in "$cert" -noout -checkend 0 >/dev/null ||
    die "сертификат ${cert} просрочен" "$hint"
  [[ "$(openssl x509 -in "$cert" -noout -checkhost "$hostname")" == *"does match"* ]] ||
    die "сертификат ${cert} выписан не на ${hostname}" "$hint"
  [[ "$(openssl x509 -in "$cert" -noout -pubkey)" == "$(openssl pkey -in "$key" -pubout 2>/dev/null)" ]] ||
    die "ключ ${key} не соответствует сертификату ${cert}" "$hint"
}

# Обе копии готовятся рядом с рабочими файлами, и только потом идут два переименования
# подряд: сбой копирования (нет места) не оставит новый ключ со старым сертификатом, а
# Caddy никогда не увидит наполовину записанный файл.
install_certificate() {
  verify_pair "$lego_cert" "$lego_key"
  install -d -m 700 "$CERTS_DIR"
  local new_key new_cert
  new_key="$(mktemp "${KEY_FILE}.XXXXXX")"
  new_cert="$(mktemp "${CERT_FILE}.XXXXXX")"
  if ! { cp "$lego_key" "$new_key" && cp "$lego_cert" "$new_cert" &&
    chmod 600 "$new_key" && chmod 644 "$new_cert"; }; then
    rm -f "$new_key" "$new_cert"
    die "не удалось скопировать сертификат и ключ в ${CERTS_DIR}" "проверить место на диске; рабочие файлы не тронуты"
  fi
  : >"$RELOAD_PENDING"
  mv "$new_key" "$KEY_FILE"
  mv "$new_cert" "$CERT_FILE"
  log "сертификат разложен: ${CERT_FILE}, ${KEY_FILE} ($(openssl x509 -in "$CERT_FILE" -noout -enddate))"
}

# --force: Caddyfile не менялся, и обычный reload ничего бы не сделал («config is
# unchanged»), а файлы сертификата Caddy читает только при загрузке конфигурации.
# Перезапуск контейнера оборвал бы идущие ответы модели.
reload_caddy() {
  if [[ -z "$(compose ps --status running -q caddy 2>/dev/null || true)" ]]; then
    log "Caddy не запущен: сертификат будет прочитан при запуске (make gateway LLM_HOSTNAME=${hostname} TLS_MODE=corp)"
    rm -f "$RELOAD_PENDING"
    return
  fi
  log "Caddy перечитывает сертификат"
  compose exec -T caddy caddy reload --config "$CADDYFILE_PATH" --force ||
    die "сертификат разложен, но Caddy его не перечитал и отдаёт прежний" "make logs SERVICE=caddy; повторить: make cert-renew (перечитывание повторит и таймер)"
  rm -f "$RELOAD_PENDING"
}

main() {
  command="${1:-}"
  manual=0
  case "$command" in
    -h | --help)
      usage
      exit 0
      ;;
    issue | renew) ;;
    *) die "ожидается issue или renew" "bash scripts/cert.sh --help" ;;
  esac
  case "${2:-}" in
    "") ;;
    --manual) manual=1 ;;
    *) die "неизвестный аргумент «$2»" "bash scripts/cert.sh --help" ;;
  esac
  local cmd
  for cmd in docker openssl; do
    command -v "$cmd" >/dev/null 2>&1 || die "не найден ${cmd}" "запустить на ВМ; sudo bash scripts/install_host.sh"
  done
  load_settings

  # К Let's Encrypt скрипт обращается, только когда это нужно; свежий, но не разложенный
  # сертификат (прошлый запуск оборвался после lego) заново не запрашивается.
  if [[ ! -f "$lego_cert" ]]; then
    [[ "$command" == "issue" ]] ||
      die "сертификат для ${hostname} ещё не выпускался" "первый выпуск: make cert"
    request_certificate "выпуск"
  elif expires_soon "$lego_cert"; then
    request_certificate "до конца срока меньше ${RENEW_DAYS} дней: продление"
  fi
  if ! deployed; then
    install_certificate
  elif [[ ! -e "$RELOAD_PENDING" ]]; then
    log "сертификат ${hostname} уже разложен ($(openssl x509 -in "$lego_cert" -noout -enddate)): продление не требуется"
    exit 0
  else
    log "сертификат разложен прошлым запуском, но Caddy его не перечитал"
  fi
  reload_caddy
  if [[ "$manual" -eq 1 ]]; then
    log "ручное подтверждение: таймер такой сертификат не продлит. Продлевать не позже чем через 60 дней: make cert-renew MANUAL=1"
  fi
}

main "$@"
