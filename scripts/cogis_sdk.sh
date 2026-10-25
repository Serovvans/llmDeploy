#!/usr/bin/env bash
# Подключение SDK CoGIS к песочнице сборки C# (docs/portal-design.md §7, §8.5;
# docs/portal-api.md §7.5.14). Обёртка — make cogis-sdk.
#
# Запуск на ВМ, от root, после make portal:
#   sudo bash scripts/cogis_sdk.sh
#
# До запуска администратор копирует с сервера CoGIS в deploy/cogis-sdk/ (вне git; это
# собственность заказчика — в git и образы не попадает):
#   eLiteGIS.Server.dll, Atemiko.eLiteMap.dll   обязательны, из /usr/elitegis/server/;
#   другие *.dll из /usr/elitegis/server/ и /usr/elitegis/plugins/ — только в корень
#                папки, подпапки не читаются;
#   plugins.txt  обязателен: список файлов папки plugins, вывод
#                ls /usr/elitegis/plugins/ > plugins.txt на сервере CoGIS.
#
# Что делает:
#   1. проверяет набор файлов папки SDK;
#   2. разовым запуском образа песочницы без сети (code-sandbox index) пишет в том
#      llm_cogis-sdk-index manifest.json (файлы, вид, версии, SHA-256) и reference.json
#      (справочник API) и печатает таблицу файлов; при ошибке ничего не записывается;
#   3. пересоздаёт code-sandbox: манифест он читает только при старте и сверяет суммы;
#   4. проверяет из portal-api, что песочница видит SDK, и печатает его версию.
# Портал перечитывает индекс сам, его перезапуск не нужен. Флаг сборки
# (code.build.enabled) скрипт не меняет. После обновления CoGIS скрипт повторяют.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../deploy" && pwd)"
readonly DEPLOY_DIR
readonly ENV_FILE="${DEPLOY_DIR}/.env"
readonly SDK_DIR="${DEPLOY_DIR}/cogis-sdk"
readonly SDK_REQUIRED=(eLiteGIS.Server.dll Atemiko.eLiteMap.dll plugins.txt)
# Имя проекта — строка name: в docker-compose.yml; с него начинаются имена томов и образов.
readonly COMPOSE_PROJECT="llm"
readonly SERVICE="code-sandbox"
readonly IMAGE_NAME="${COMPOSE_PROJECT}-${SERVICE}"
readonly INDEX_VOLUME_KEY="cogis-sdk-index"
readonly INDEX_VOLUME="${COMPOSE_PROJECT}_${INDEX_VOLUME_KEY}"
readonly HEALTH_URL="http://${SERVICE}:8080/health"
# Печатает sdk_version из GET /health песочницы; в образе portal-api нет curl.
readonly HEALTH_PROBE="import json, urllib.request; print(json.load(urllib.request.urlopen('${HEALTH_URL}', timeout=10)).get('sdk_version') or '')"

log() {
  echo "==> $1"
}

die() {
  echo "ОШИБКА: $1" >&2
  echo "что делать: $2" >&2
  exit 1
}

compose() {
  docker compose -f "${DEPLOY_DIR}/docker-compose.yml" "$@"
}

# Значение NAME из deploy/.env (последняя строка NAME=...), без окружающих кавычек.
env_file_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^[\"']//" -e "s/[\"']\$//"
}

check_root() {
  [[ "$EUID" -eq 0 ]] || die "нужны права root (docker)" "sudo make cogis-sdk"
}

check_portal_deployed() {
  [[ -f "$ENV_FILE" ]] || die "нет ${ENV_FILE}" "сначала развернуть портал: make portal"
  [[ ",$(env_file_value COMPOSE_PROFILES)," == *",portal,"* ]] ||
    die "портал не развёрнут: в COMPOSE_PROFILES нет portal" "сначала make portal"
}

check_sdk_dir() {
  local fix="скопировать файлы с сервера CoGIS в ${SDK_DIR}/ (заголовок scripts/cogis_sdk.sh)"
  [[ -d "$SDK_DIR" ]] || die "нет папки ${SDK_DIR}" "$fix"
  local name missing=()
  for name in "${SDK_REQUIRED[@]}"; do
    [[ -f "${SDK_DIR}/${name}" ]] || missing+=("$name")
  done
  [[ "${#missing[@]}" -eq 0 ]] ||
    die "в ${SDK_DIR} нет ${missing[*]}" \
      "${fix}; plugins.txt — вывод ls /usr/elitegis/plugins/ на сервере CoGIS; имена — с учётом регистра"
  local entry subdirs=()
  for entry in "$SDK_DIR"/*/; do
    if [[ -d "$entry" ]]; then subdirs+=("$(basename "$entry")"); fi
  done
  [[ "${#subdirs[@]}" -eq 0 ]] ||
    log "ВНИМАНИЕ: подпапки ${SDK_DIR} не читаются (${subdirs[*]}): DLL кладутся в корень папки"
  # Пустой список допустим (плагинов нет), но чаще значит, что файл сняли не с той папки.
  grep -qiE '\.dll[[:space:]]*$' "${SDK_DIR}/plugins.txt" ||
    log "ВНИМАНИЕ: в plugins.txt нет ни одного .dll — все DLL пакетов попадут в архив для CoGIS; проверить: ls /usr/elitegis/plugins/ на сервере CoGIS"
}

image_ref() {
  local image
  image="${IMAGE_NAME}:$(env_file_value PORTAL_IMAGE_TAG)"
  docker image inspect "$image" >/dev/null 2>&1 ||
    die "нет образа ${image}" "образ собирает установка портала: make portal"
  printf '%s' "$image"
}

# Том создаётся раньше compose, если песочница ещё не запускалась; метки — чтобы compose
# не предупреждал, что том создан не им. Владельца каталога /index (пользователь app)
# новый том берёт из образа при первом подключении.
ensure_index_volume() {
  docker volume inspect "$INDEX_VOLUME" >/dev/null 2>&1 ||
    docker volume create --label "com.docker.compose.project=${COMPOSE_PROJECT}" \
      --label "com.docker.compose.volume=${INDEX_VOLUME_KEY}" "$INDEX_VOLUME" >/dev/null
}

# Те же ограничения, что у сервиса в compose, но без сети и с томом индекса на запись.
build_index() {
  local image="$1"
  log "индекс SDK: ${SDK_DIR} → том ${INDEX_VOLUME}"
  docker run --rm --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true --tmpfs /tmp:size=16m,noexec,nosuid,nodev \
    -v "${SDK_DIR}:/sdk:ro" -v "${INDEX_VOLUME}:/index" \
    "$image" index --sdk /sdk --out /index ||
    die "индекс SDK не построен (причина — в строках error: выше), прежний индекс не изменён" \
      "исправить набор файлов в ${SDK_DIR} и повторить make cogis-sdk; если сказано о правах на /index — docker volume rm ${INDEX_VOLUME} (сначала docker compose stop ${SERVICE} portal-api) и повторить"
}

restart_sandbox() {
  log "пересоздание ${SERVICE}: SDK и индекс читаются при старте"
  compose up -d --no-deps --force-recreate --wait "$SERVICE" ||
    die "${SERVICE} не запустился с новым SDK" "cd ${DEPLOY_DIR} && docker compose logs ${SERVICE}"
}

# Версия SDK — в sdk_version.
verify_sdk() {
  sdk_version="$(compose exec -T portal-api python -c "$HEALTH_PROBE")" ||
    die "portal-api не получил ответ ${HEALTH_URL}" \
      "cd ${DEPLOY_DIR} && docker compose ps ${SERVICE} portal-api; docker compose logs ${SERVICE}"
  [[ -n "$sdk_version" ]] ||
    die "песочница не видит SDK: sdk_version пуст — файлы ${SDK_DIR} разошлись с манифестом" \
      "не менять файлы после индекса; повторить make cogis-sdk; журнал: cd ${DEPLOY_DIR} && docker compose logs ${SERVICE}"
  log "проверка: песочница видит SDK CoGIS ${sdk_version}"
}

print_summary() {
  cat <<EOF

===== SDK CoGIS подключён: версия ${sdk_version} =====
Флаг сборки не менялся. Пока сборка выключена, «Собрать» недоступно; включить её —
после проверки сборки на настоящих DLL (docs/portal-design.md §8.5):
  в ${DEPLOY_DIR}/portal/config.override.yaml задать
    code:
      build:
        enabled: true
  и перезапустить портал: cd ${DEPLOY_DIR} && docker compose restart portal-api
После обновления CoGIS — заменить файлы в ${SDK_DIR} и повторить make cogis-sdk.
EOF
}

main() {
  check_root
  check_portal_deployed
  check_sdk_dir
  local image
  image="$(image_ref)"
  ensure_index_volume
  build_index "$image"
  restart_sandbox
  verify_sdk
  print_summary
}

# При source (тесты) функции только определяются.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
