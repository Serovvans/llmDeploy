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
#                ls /usr/elitegis/plugins/ > plugins.txt на сервере CoGIS;
#   <id>.dll     каждого пакета офлайн-набора, чья DLL названа в plugins.txt, — копия
#                из /usr/elitegis/plugins/ (portal-api.md §7.5.14, «Пакеты на сервере»);
#   client/      (1.0, необязательна) файлы клиента CoGIS для предпросмотра и подсказок
#                под постоянными именами: jquery.js, knockout.js, leaflet.js, leaflet.css;
#                каждый необязателен (portal-api.md §7.5.16, «Библиотеки клиента CoGIS»).
#
# Что делает:
#   1. проверяет набор файлов папки SDK, файлы client/ (не больше 4 МиБ, UTF-8) и копии
#      DLL пакетов, стоящих на сервере (список пакетов — portal code-packages разовым
#      запуском образа portal-api без сети);
#   2. разовым запуском образа песочницы без сети (code-sandbox index) пишет в том
#      llm_cogis-sdk-index manifest.json (файлы, вид, версии, SHA-256) и reference.json
#      (справочник API) и печатает таблицу файлов; при ошибке ничего не записывается;
#   3. заменяет каталог client/ тома индекса копиями файлов client/ (нет папки — пустой
#      каталог) тем же образом без сети;
#   4. пересоздаёт code-sandbox: манифест он читает только при старте и сверяет суммы;
#   5. проверяет из portal-api, что песочница видит SDK, и печатает его версию и версии
#      библиотек клиента CoGIS.
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
# Библиотеки клиента CoGIS (1.0): папка, постоянные имена файлов и предел размера.
readonly CLIENT_DIR="${SDK_DIR}/client"
readonly CLIENT_FILES=(jquery.js knockout.js leaflet.js leaflet.css)
readonly CLIENT_MAX_BYTES=$((4 * 1024 * 1024))
# Замена client/ в томе индекса (внутри разового контейнера): распаковка tar-потока в
# client.new, права 644 для portal-api, подмена прежнего каталога.
readonly CLIENT_REPLACE='set -eu
cd /index
rm -rf client.new
mkdir client.new
tar -xf - -C client.new
find client.new -type f -exec chmod 644 {} +
rm -rf client
mv client.new client'
# Имя проекта — строка name: в docker-compose.yml; с него начинаются имена томов и образов.
readonly COMPOSE_PROJECT="llm"
readonly SERVICE="code-sandbox"
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
    if [[ -d "$entry" && "$(basename "$entry")" != client ]]; then
      subdirs+=("$(basename "$entry")")
    fi
  done
  [[ "${#subdirs[@]}" -eq 0 ]] ||
    log "ВНИМАНИЕ: подпапки ${SDK_DIR} не читаются (${subdirs[*]}): DLL кладутся в корень папки"
  # Пустой список допустим (плагинов нет), но чаще значит, что файл сняли не с той папки.
  grep -qiE '\.dll[[:space:]]*$' "${SDK_DIR}/plugins.txt" ||
    log "ВНИМАНИЕ: в plugins.txt нет ни одного .dll — все DLL пакетов попадут в архив для CoGIS; проверить: ls /usr/elitegis/plugins/ на сервере CoGIS"
}

is_client_file() {
  local name
  for name in "${CLIENT_FILES[@]}"; do
    [[ "$1" == "$name" ]] && return 0
  done
  return 1
}

# Файлы client/, которые копируются в том индекса: известные имена, по строке на файл.
client_present() {
  local name
  for name in "${CLIENT_FILES[@]}"; do
    if [[ -f "${CLIENT_DIR}/${name}" ]]; then printf '%s\n' "$name"; fi
  done
}

# Файлы client/ проверяются до индекса: при отказе том не меняется. Папки нет — не ошибка.
check_client_dir() {
  [[ -d "$CLIENT_DIR" ]] || return 0
  local entry name size others=()
  for entry in "$CLIENT_DIR"/*; do
    [[ -e "$entry" ]] || continue
    name="${entry##*/}"
    if [[ ! -f "$entry" ]] || ! is_client_file "$name"; then others+=("$name"); fi
  done
  [[ "${#others[@]}" -eq 0 ]] ||
    log "ВНИМАНИЕ: в ${CLIENT_DIR} не копируются (${others[*]}): берутся только ${CLIENT_FILES[*]}"
  while IFS= read -r name; do
    size="$(wc -c <"${CLIENT_DIR}/${name}" | tr -d '[:space:]')"
    ((size <= CLIENT_MAX_BYTES)) ||
      die "${CLIENT_DIR}/${name} больше 4 МиБ (${size} байт)" \
        "взять с сервера CoGIS файл самой библиотеки (не архив и не сборку клиента целиком) и повторить make cogis-sdk"
    iconv -f UTF-8 -t UTF-8 "${CLIENT_DIR}/${name}" >/dev/null 2>&1 ||
      die "${CLIENT_DIR}/${name} не в кодировке UTF-8" \
        "скопировать файл с сервера CoGIS без преобразования (scp, не через буфер обмена) и повторить make cogis-sdk"
  done < <(client_present)
}

# Версия из первых 2 КиБ файла $1 по образцу $2 (как у portal-api, portal-api.md §7.5.16);
# пусто — не нашлась.
client_version() {
  head -c 2048 "$1" | LC_ALL=C grep -oE "$2" | head -n 1 |
    grep -oE '[0-9]+\.[0-9]+\.[0-9]+' || true
}

# «Библиотека версия», «Библиотека (версия не найдена)» или «Библиотека нет».
client_entry() {
  local title="$1" file="${CLIENT_DIR}/$2" pattern="$3" version
  if [[ ! -f "$file" ]]; then
    printf '%s нет' "$title"
    return
  fi
  version="$(client_version "$file" "$pattern")"
  printf '%s %s' "$title" "${version:-(версия не найдена)}"
}

client_summary() {
  local css="нет"
  if [[ -f "${CLIENT_DIR}/leaflet.css" ]]; then css="есть"; fi
  printf 'Библиотеки клиента CoGIS: %s, %s, %s, leaflet.css %s\n' \
    "$(client_entry jQuery jquery.js 'jQuery (JavaScript Library )?v[0-9]+\.[0-9]+\.[0-9]+')" \
    "$(client_entry Knockout knockout.js 'Knockout JavaScript library v[0-9]+\.[0-9]+\.[0-9]+')" \
    "$(client_entry Leaflet leaflet.js 'Leaflet [0-9]+\.[0-9]+\.[0-9]+')" \
    "$css"
}

# Образ сервиса $1 с тегом установки портала.
image_ref() {
  local image
  image="${COMPOSE_PROJECT}-$1:$(env_file_value PORTAL_IMAGE_TAG)"
  docker image inspect "$image" >/dev/null 2>&1 ||
    die "нет образа ${image}" "образ собирает установка портала: make portal"
  printf '%s' "$image"
}

lower() {
  tr '[:upper:]' '[:lower:]'
}

# Офлайн-набор code.packages из образа portal-api (как у make code-packages): строка
# «id<TAB>version<TAB>sha512» на пакет. Пароль нужен загрузке настроек портала, к базе
# команда не обращается; docker берёт его из окружения, в argv его нет.
list_packages() {
  local image="$1" err out
  err="$(mktemp)"
  if ! out="$(PORTAL_DB_PASSWORD="$(env_file_value PORTAL_DB_PASSWORD)" \
    docker run --rm --network none -e PORTAL_DB_PASSWORD \
    -v "${DEPLOY_DIR}/portal:/etc/portal:ro" "$image" portal code-packages 2>"$err")"; then
    local reason
    reason="$(cat "$err")"
    rm -f "$err"
    die "portal code-packages не выдал офлайн-набор: ${reason}" \
      "проверить code.packages в ${DEPLOY_DIR}/portal/config.override.yaml и PORTAL_DB_PASSWORD в ${ENV_FILE}; повторить make cogis-sdk"
  fi
  rm -f "$err"
  printf '%s\n' "$out"
}

# Пакет набора, чья <id>.dll названа в plugins.txt, требует копии этой DLL в корне папки
# SDK: иначе .csproj выгрузки для Visual Studio на неё не ссылается (А-119). Имена — без
# учёта регистра, строки plugins.txt — без пробелов по краям.
check_server_packages() {
  local ids files name lname id missing=()
  ids="$(list_packages "$1" | cut -f1 | lower)"
  files="$(for name in "$SDK_DIR"/*; do
    if [[ -f "$name" ]]; then printf '%s\n' "${name##*/}"; fi
  done | lower)"
  while IFS= read -r name; do
    lname="$(printf '%s' "$name" | lower)"
    id="${lname%.dll}"
    if [[ -n "$id" ]] && grep -qxF -- "$id" <<<"$ids" && ! grep -qxF -- "$lname" <<<"$files"; then
      missing+=("$name")
    fi
  done < <(awk '{ gsub(/^[[:space:]]+|[[:space:]]+$/, "") } tolower($0) ~ /\.dll$/' \
    "${SDK_DIR}/plugins.txt")
  [[ "${#missing[@]}" -eq 0 ]] ||
    die "DLL пакетов, которые уже стоят на сервере CoGIS (есть в plugins.txt), нет в ${SDK_DIR}: ${missing[*]}" \
      "скопировать эти файлы с сервера CoGIS из /usr/elitegis/plugins/ в ${SDK_DIR}/ и повторить make cogis-sdk"
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

# Файлы идут в контейнер tar-потоком со стороны ВМ: их читает root, и права копий
# администратора не мешают пользователю образа. Нет файлов — пустой каталог client/.
copy_client() {
  local image="$1" name files=()
  while IFS= read -r name; do files+=("$name"); done < <(client_present)
  log "библиотеки клиента CoGIS: ${CLIENT_DIR} → том ${INDEX_VOLUME}, каталог client/ (${files[*]:-файлов нет})"
  {
    if [[ "${#files[@]}" -gt 0 ]]; then
      tar -C "$CLIENT_DIR" -cf - -- "${files[@]}"
    else
      tar -cf - -T /dev/null
    fi
  } | docker run --rm -i --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges:true -v "${INDEX_VOLUME}:/index" \
    --entrypoint /bin/sh "$image" -c "$CLIENT_REPLACE" ||
    die "библиотеки клиента CoGIS не скопированы в том ${INDEX_VOLUME} (индекс SDK уже обновлён)" \
      "повторить make cogis-sdk; если сказано о правах на /index — docker volume rm ${INDEX_VOLUME} (сначала docker compose stop ${SERVICE} portal-api) и повторить"
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
$(client_summary)
Флаг сборки не менялся. Пока сборка выключена, «Собрать» недоступно; включить её —
после проверки сборки на настоящих DLL (docs/portal-design.md §8.5):
  в ${DEPLOY_DIR}/portal/config.override.yaml задать
    code:
      build:
        enabled: true
  и перезапустить портал: cd ${DEPLOY_DIR} && docker compose restart portal-api
После обновления CoGIS — заменить файлы в ${SDK_DIR} (и в ${CLIENT_DIR}) и повторить
make cogis-sdk.
EOF
}

main() {
  check_root
  check_portal_deployed
  check_sdk_dir
  check_client_dir
  local image api_image
  image="$(image_ref "$SERVICE")"
  api_image="$(image_ref portal-api)"
  check_server_packages "$api_image"
  ensure_index_volume
  build_index "$image"
  copy_client "$image"
  restart_sandbox
  verify_sdk
  print_summary
}

# При source (тесты) функции только определяются.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
