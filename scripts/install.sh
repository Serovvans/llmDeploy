#!/usr/bin/env bash
# Установка и запуск LLM-сервиса на подготовленной ВМ (docs/design.md §9, docs/runbook.md §2).
#
# Развёртывание идёт этапами (docs/design.md §3.7, §3.8); обёртки — в Makefile (make help).
# Запуск из клона репозитория на ВМ, после scripts/install_host.sh и перезагрузки:
#
#   sudo bash scripts/install.sh --stage model
#       Этап 1 — только модель: vLLM (+ выбранные профили). DNS-имя и сертификат не
#       нужны; модель доступна на 127.0.0.1:8000 для смоук-теста и бенчмарка.
#
#   sudo bash scripts/install.sh --stage gateway --hostname llm.corp.example --tls corp
#       Этап 2 — внешний доступ: добавляет профиль gateway (Caddy + Bifrost). Caddy
#       слушает 443, 8080 и 18443 (docs/portal-design.md §3.2). Сертификат Let's Encrypt
#       для --tls corp выпускается заранее отдельным шагом: make cert (scripts/cert.sh).
#
#   sudo bash scripts/install.sh --stage portal
#       Этап 3 — портал сотрудников (docs/portal-design.md §3, §7): добавляет профиль
#       portal. Требует уже включённых gateway и embeddings и ключа PORTAL_LLM_API_KEY
#       в deploy/.env (выдать: make key NAME=portal RERANK=1).
#
#   sudo bash scripts/install.sh --prepare-from /opt/llm --profiles embeddings,rerank
#       Подготовка релиза на работающей ВМ (docs/portal-design.md §8.2). Запускается из
#       отдельного клона релизного коммита; /opt/llm — рабочий клон. Ни работающие
#       контейнеры, ни рабочий клон не меняются: его .env копируется сюда на время
#       запуска и удаляется по завершении. Делает шаги 1, 2 и 4–5 списка ниже и
#       останавливается; нынешние образы портала до сборки получают тег коммита
#       рабочего клона — это путь отката. В окне обслуживания тот же набор аргументов
#       (без --prepare-from) передаётся установке в рабочем клоне, переключённом на
#       релизный коммит: образы уже собраны, сборки в окне нет. Остальные шаги релиза —
#       scripts/release.sh.
#
# Аргументы (при повторном запуске берутся из deploy/.env, а заданные явно —
# перезаписывают значение в .env):
#   --stage model|gateway|portal
#                      этап; по умолчанию — тот, что уже записан в deploy/.env.
#                      gateway и portal только добавляют профиль: чтобы вернуться назад,
#                      уберите профиль из COMPOSE_PROFILES в deploy/.env (для portal —
#                      ещё и очистите SITE_MODE)
#   --hostname NAME    DNS-имя сервиса или, до привязки домена, IP-адрес ВМ
#                      (LLM_HOSTNAME); нужно на этапе gateway
#   --tls MODE         corp — сертификат в deploy/certs/{fullchain,privkey}.pem;
#                      internal — собственный CA Caddy (TLS_MODE); нужно на этапе gateway
#   --profiles LIST    дополнительные профили через запятую: embeddings, monitoring,
#                      rerank (реранкер поиска портала, только вместе с embeddings);
#                      "" — ни одного (COMPOSE_PROFILES)
#   --gpu-mem-util N   доля видеопамяти основной модели (GPU_MEM_UTIL); с профилем
#                      rerank — 0.81 (docs/design.md §3.9)
#   --prepare-from DIR подготовка релиза из отдельного клона; DIR — рабочий клон
#   --skip-preflight   не запускать scripts/preflight.sh (например, при повторном запуске)
#
# Что делает:
#   1. preflight.sh (при подготовке релиза — preflight.sh --release);
#   2. deploy/.env из .env.example; пустые секреты генерируются, заданные не меняются.
#      Переменные, которых в существующем .env нет (после git pull), дописываются со
#      значениями из .env.example, секреты портала и песочницы SQL — на любом этапе:
#      compose интерполирует сервисы и выключенных профилей, без них не выполнится ни
#      одна команда docker compose. PORTAL_IMAGE_TAG — короткий хеш коммита клона. Перед
#      сменой тега работающего портала прежний .env сохраняется в deploy/rollback/ —
#      в подкаталоге с прежним тегом, поэтому повторная установка с другого коммита в
#      том же окне копию не затирает;
#   3. (gateway + corp) проверка сертификата в deploy/certs/: срок, имя хоста,
#      соответствие ключу. Сам сертификат скрипт не выпускает;
#   4. docker compose config и pull; (portal) сборка образов portal-api, portal-worker,
#      portal-web и sql-sandbox из этого клона с тегом PORTAL_IMAGE_TAG. Сборка
#      пропускается, только если образы с тегом этого коммита уже есть и в коде
#      портала нет незакоммиченных правок;
#   5. предзагрузка весов моделей в volume hf-cache (основная, эмбеддинги, реранкер) и
#      весов модели речи в volume asr-models;
#   6. systemd-юнит llm-stack с путём к этому клону; запуск: сначала модели, затем
#      остальные сервисы, ожидание healthy; (gateway) Bifrost и Caddy пересоздаются,
#      чтобы применить config.json и Caddyfile этого клона;
#      (gateway + corp, сертификат выпущен make cert) таймер продления llm-cert-renew;
#   7. (gateway + internal) выгрузка корневого сертификата Caddy в deploy/caddy-root.crt;
#   8. проверка: этап model — на 127.0.0.1:8000 запрос без ключа даёт 401, с ключом
#      модель отвечает; этап gateway — через 443 запрос без ключа 401/403, / — 404,
#      закрытые маршруты под /v1 (включая /v1/rerank) — 404,
#      на 8080 и 18443 отвечает тот же сайт;
#      этап portal — через 443 / отдаёт страницу входа, /api/auth/session без сессии —
#      401, админ-API Bifrost и /metrics — 404, запрос к модели без ключа — 401/403.
# Идемпотентен: повторный запуск применяет изменения .env и compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
DEPLOY_DIR="$(cd "${SCRIPT_DIR}/../deploy" && pwd)"
readonly DEPLOY_DIR
REPO_DIR="$(dirname "$DEPLOY_DIR")"
readonly REPO_DIR
readonly ENV_FILE="${DEPLOY_DIR}/.env"
readonly ENV_EXAMPLE="${DEPLOY_DIR}/.env.example"
readonly CERTS_DIR="${DEPLOY_DIR}/certs"
readonly ROOT_CERT_FILE="${DEPLOY_DIR}/caddy-root.crt"
readonly UNIT_SOURCE="${DEPLOY_DIR}/systemd/llm-stack.service"
readonly UNIT_TARGET="/etc/systemd/system/llm-stack.service"
# Таймер продления сертификата Let's Encrypt (scripts/cert.sh renew) и состояние lego.
readonly SYSTEMD_DIR="/etc/systemd/system"
readonly RENEW_UNITS=("${DEPLOY_DIR}/systemd/llm-cert-renew.service" "${DEPLOY_DIR}/systemd/llm-cert-renew.timer")
readonly RENEW_TIMER="llm-cert-renew.timer"
readonly LEGO_CERTS_DIR="${DEPLOY_DIR}/lego/certificates"
# Порты хоста помимо 443, на которых Caddy отдаёт тот же сайт: цель проброса из
# интернета и тот же адрес из сети заказчика (docs/portal-design.md §3.2).
readonly CADDY_EXTRA_PORTS=(8080 18443)
# Админ-порт Bifrost на хосте, только loopback: 8080 занят Caddy.
readonly BIFROST_ADMIN_PORT=8081
readonly CADDY_ROOT_CERT_PATH="/data/caddy/pki/authorities/local/root.crt"
readonly DEFAULT_ADMIN_USERNAME="admin"
readonly CERT_WARN_DAYS=30
readonly HOSTNAME_RE='^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$'
readonly PROFILES_RE='^((embeddings|monitoring|rerank)(,(embeddings|monitoring|rerank)){0,2})?$'
# Профиль второго этапа: Caddy и Bifrost (docs/design.md §3.7).
readonly GATEWAY_PROFILE="gateway"
# Профиль третьего этапа: портал сотрудников (docs/portal-design.md §3).
readonly PORTAL_PROFILE="portal"
# Образы этих сервисов собираются из клона: сами после git pull они не пересоберутся.
# portal-api и portal-worker — один код в двух образах, поэтому собираются одной командой.
readonly PORTAL_BUILD_SERVICES=(portal-api portal-worker portal-web sql-sandbox)
# Имя проекта — строка name: в docker-compose.yml; с него начинаются имена томов и
# образов (llm_asr-models, llm-portal-api:<тег>).
readonly COMPOSE_PROJECT="llm"
readonly IMAGE_PREFIX="${COMPOSE_PROJECT}-"
# Доля видеопамяти эмбеддингов задана в docker-compose.yml (vllm-embed); сумма долей
# всех экземпляров на карте — не больше 0.92 (docs/design.md §3.6, §3.9).
readonly EMBED_GPU_MEM_UTIL="0.06"
readonly MAX_GPU_MEM_UTIL_SUM="0.92"
readonly GPU_MEM_UTIL_RE='^0\.[0-9]+$'
# Веса распознавания речи (docs/portal-design.md §3.3): подкаталог тома, репозиторий
# Hugging Face, закреплённая ревизия, файлы. Подкаталоги и состав файлов — те, что ждёт
# воркер (portal/backend/src/portal/media/gigaam.py); варианты *.int8.onnx не нужны.
readonly ASR_VOLUME="${COMPOSE_PROJECT}_asr-models"
readonly ASR_MODELS=(
  "gigaam-v3-e2e-rnnt istupakov/gigaam-v3-onnx 322c3b29492673eb7d0b434bfa9dfb8653e34d02 config.json v3_e2e_rnnt_encoder.onnx v3_e2e_rnnt_decoder.onnx v3_e2e_rnnt_joint.onnx v3_e2e_rnnt_vocab.txt"
  "silero istupakov/silero-vad-onnx b3e3ee3cce4c11ceb63b1a0b229d916069c1ddf6 config.json silero_vad.onnx"
)
readonly VLLM_LOCAL_URL="http://127.0.0.1:8000"
# Под /v1 Caddy проксирует в Bifrost только /v1/chat/completions, /v1/completions,
# /v1/embeddings и /v1/models (docs/design.md §3.3); эти запросы без ключа должны
# получать 404 от Caddy при любом SITE_MODE. /v1/rerank — только для портала по
# docker-сети (§3.9). Тот же список — в smoke_test.py.
readonly V1_CLOSED_ROUTES=(
  "GET /v1/unknown-route" "GET /v1/mcp/tools" "GET /v1/skills" "GET /v1" "GET /v1/"
  "POST /v1/async/chat/completions" "POST /v1/mcp/tool/execute" "POST /v1/rerank"
)
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

# Чтение клона без записи в него. safe.directory: скрипт идёт от root, а клон принадлежит
# администратору — иначе git отказывается работать с чужим репозиторием.
# --no-optional-locks: status не переписывает индекс, и в .git не появляется файлов root.
repo_git() {
  local dir="$1"
  shift
  git --no-optional-locks -c safe.directory='*' -C "$dir" "$@"
}

# Тег образов портала — короткий хеш коммита клона (docs/portal-design.md §8.2). Длина
# задана явно: по умолчанию git подбирает её по числу объектов, и у релизного клона тег
# того же коммита вышел бы другим.
commit_tag() {
  repo_git "$1" rev-parse --short=12 HEAD 2>/dev/null ||
    die "не удалось определить коммит клона $1" "скрипт запускается из клона git-репозитория; проверить: git -C $1 log -1"
}

parse_args() {
  arg_stage=""
  arg_hostname=""
  arg_tls=""
  arg_profiles=""
  arg_gpu_mem_util=""
  live_dir=""
  profiles_given=0
  prepare_mode=0
  skip_preflight=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --stage | --hostname | --tls | --profiles | --gpu-mem-util | --prepare-from)
        [[ $# -ge 2 ]] || die "$1 требует значение" "bash scripts/install.sh --help"
        case "$1" in
          --stage) arg_stage="$2" ;;
          --hostname) arg_hostname="$2" ;;
          --tls) arg_tls="$2" ;;
          --profiles) arg_profiles="$2"; profiles_given=1 ;;
          --gpu-mem-util) arg_gpu_mem_util="$2" ;;
          --prepare-from) live_dir="$2"; prepare_mode=1 ;;
        esac
        shift 2
        ;;
      --skip-preflight) skip_preflight=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "неизвестный аргумент «$1»" "bash scripts/install.sh --help" ;;
    esac
  done
  [[ -z "$arg_stage" || "$arg_stage" =~ ^(model|gateway|portal)$ ]] ||
    die "--stage «${arg_stage}»: ожидается model, gateway или portal" \
      "--stage model — только модель, gateway — добавить внешний доступ, portal — добавить портал (docs/design.md §3.7, §3.8)"
  [[ -z "$arg_gpu_mem_util" || "$arg_gpu_mem_util" =~ $GPU_MEM_UTIL_RE ]] ||
    die "--gpu-mem-util «${arg_gpu_mem_util}»: ожидается доля вида 0.81" \
      "доля видеопамяти основной модели: 0.85 без реранкера, 0.81 с ним (docs/design.md §3.9)"
}

check_system() {
  [[ "$(uname -s)" == "Linux" ]] || die "скрипт запускается на ВМ с Ubuntu" "запустить на ВМ"
  [[ "$EUID" -eq 0 ]] || die "нужны права root (docker, systemd)" "sudo bash scripts/install.sh ..."
  local cmd
  for cmd in docker openssl curl systemctl git; do
    command -v "$cmd" >/dev/null 2>&1 || die "не найден ${cmd}" "sudo bash scripts/install_host.sh"
  done
}

run_preflight() {
  if [[ "$skip_preflight" -eq 1 ]]; then
    log "preflight пропущен (--skip-preflight)"
    return
  fi
  log "preflight"
  local release_arg=""
  [[ "$prepare_mode" -eq 0 ]] || release_arg="--release"
  bash "${SCRIPT_DIR}/preflight.sh" ${release_arg:+"$release_arg"} ||
    die "preflight не прошёл" "исправить пункты FAIL из сводки выше и повторить установку"
}

# Убирает профили этапов (gateway, portal) из списка, оставляя дополнительные
# (embeddings, monitoring, rerank).
without_stage_profiles() {
  local list=",$1,"
  list="${list//,${GATEWAY_PROFILE},/,}"
  list="${list//,${PORTAL_PROFILE},/,}"
  list="${list#,}"
  printf '%s' "${list%,}"
}

# Определяет этап и итоговый COMPOSE_PROFILES: дополнительные профили из --profiles
# (иначе сохраняются из .env) плюс gateway и portal, если они уже включены или их
# включает --stage. Этап только добавляет: обратно — правкой .env вручную.
resolve_stage() {
  local current extras
  current="$(env_file_value COMPOSE_PROFILES)"

  gateway_enabled=0
  [[ ",${current}," == *",${GATEWAY_PROFILE},"* ]] && gateway_enabled=1
  [[ "$arg_stage" == "gateway" ]] && gateway_enabled=1
  portal_enabled=0
  [[ ",${current}," == *",${PORTAL_PROFILE},"* ]] && portal_enabled=1
  [[ "$arg_stage" == "portal" ]] && portal_enabled=1

  if [[ "$profiles_given" -eq 1 ]]; then
    extras="$arg_profiles"
  else
    extras="$(without_stage_profiles "$current")"
  fi
  [[ "$extras" =~ $PROFILES_RE ]] ||
    die "--profiles «${extras}»: допустимы пусто и embeddings, monitoring, rerank через запятую" \
      "внешний доступ включается не здесь, а через --stage gateway, портал — через --stage portal"
  # vllm-rerank запускается после vllm-embed: без него compose не соберёт конфигурацию.
  [[ ",${extras}," != *",rerank,"* || ",${extras}," == *",embeddings,"* ]] ||
    die "профиль rerank требует профиль embeddings, а в наборе профилей «${extras}» его нет" \
      "указать оба: PROFILES=embeddings,rerank (docs/design.md §3.9)"

  if [[ "$portal_enabled" -eq 1 ]]; then
    [[ "$gateway_enabled" -eq 1 ]] ||
      die "портал требует профиль gateway, а внешний доступ ещё не включён" \
        "сначала этап 2: make gateway LLM_HOSTNAME=<имя или IP> TLS_MODE=corp|internal PROFILES=embeddings"
    [[ ",${extras}," == *",embeddings,"* ]] ||
      die "портал требует профиль embeddings (база знаний), а в наборе профилей «${extras}» его нет" \
        "добавить профиль: make portal PROFILES=embeddings (с мониторингом — PROFILES=embeddings,monitoring)"
  fi

  profiles="$extras"
  [[ "$gateway_enabled" -eq 0 ]] || profiles="${GATEWAY_PROFILE}${extras:+,${extras}}"
  [[ "$portal_enabled" -eq 0 ]] || profiles="${profiles},${PORTAL_PROFILE}"
  if [[ "$portal_enabled" -eq 1 ]]; then
    log "этап portal: модель + внешний доступ + портал сотрудников"
  elif [[ "$gateway_enabled" -eq 1 ]]; then
    log "этап gateway: модель + внешний доступ (Caddy, Bifrost)"
  else
    log "этап model: только модель на ${VLLM_LOCAL_URL}, без внешнего доступа"
  fi
}

# Подготовка релиза: .env рабочего клона копируется в этот клон на время запуска —
# рабочий клон и его .env не меняются (docs/portal-design.md §8.2).
copy_live_env() {
  local live_env="${live_dir}/deploy/.env"
  [[ -f "$live_env" ]] ||
    die "нет ${live_env}" "--prepare-from принимает каталог рабочего клона, из которого запущен сервис"
  [[ ! -e "$ENV_FILE" ]] ||
    die "подготовка релиза запускается из отдельного клона без своего deploy/.env, а здесь есть ${ENV_FILE}" \
      "создать клон релизного коммита рядом с рабочим (git clone или git worktree add) и запустить из него; если ${ENV_FILE} остался от прерванной подготовки — удалить его"
  trap 'rm -f "$ENV_FILE"' EXIT
  install -m 600 "$live_env" "$ENV_FILE"
  log "подготовка релиза: .env взят из ${live_dir}, рабочий клон не меняется"
}

# Дописывает в существующий .env переменные, которых в нём нет: после git pull в
# .env.example появляются новые, а compose требует их и при выключенном профиле.
# Значение — из образца; строки, которые в .env уже есть, не меняются.
add_missing_variables() {
  local line name
  while IFS= read -r line; do
    name="${line%%=*}"
    if ! grep -q "^${name}=" "$ENV_FILE"; then
      set_env_value "$name" "${line#*=}"
      log "в ${ENV_FILE} добавлена переменная ${name}"
    fi
  done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_EXAMPLE")
}

# Путь копии .env релиза с тегом образов $2 в клоне $1. У каждого релиза своя копия:
# установка следующего коммита в том же окне (исправление неудачного релиза) не затирает
# .env релиза, работавшего до окна. В .env версии до 0.5 тега нет — её копия лежит без
# подкаталога. Файл назван .env: это имя уже закрыто .gitignore.
rollback_env_path() {
  printf '%s/deploy/rollback/%s.env' "$1" "${2:+$2/}"
}

# Перед тем как у работающего портала сменится тег образов, прежний .env сохраняется:
# по нему scripts/release.sh rollback возвращает профили, долю памяти и образы.
save_rollback_env() {
  local new_tag="$1" old_tag saved
  [[ "$prepare_mode" -eq 0 ]] || return 0
  [[ ",$(env_file_value COMPOSE_PROFILES)," == *",${PORTAL_PROFILE},"* ]] || return 0
  old_tag="$(env_file_value PORTAL_IMAGE_TAG)"
  [[ "$old_tag" != "$new_tag" ]] || return 0
  saved="$(rollback_env_path "$REPO_DIR" "$old_tag")"
  install -d -m 700 "${DEPLOY_DIR}/rollback" "$(dirname "$saved")"
  install -m 600 "$ENV_FILE" "$saved"
  log "прежний .env сохранён в ${saved} (откат релиза — из релизного клона: make release-rollback LIVE=${REPO_DIR})"
}

# Значение NAME из deploy/.env, а если строки там ещё нет (переменная появилась в новом
# релизе и будет дописана) — из .env.example.
env_or_example_value() {
  if grep -q "^$1=" "$ENV_FILE"; then
    env_file_value "$1"
  else
    sed -n "s/^$1=//p" "$ENV_EXAMPLE" | tail -n 1
  fi
}

# Сумма долей видеопамяти экземпляров vLLM включённых профилей (docs/design.md §3.6).
# Считается по итоговым значениям до записи в .env: --gpu-mem-util важнее файла.
check_gpu_shares() {
  local main embed=0 rerank=0
  main="${arg_gpu_mem_util:-$(env_or_example_value GPU_MEM_UTIL)}"
  [[ ",${profiles}," != *",embeddings,"* ]] || embed="$EMBED_GPU_MEM_UTIL"
  [[ ",${profiles}," != *",rerank,"* ]] || rerank="$(env_or_example_value RERANK_GPU_MEM_UTIL)"
  awk -v main="$main" -v embed="$embed" -v rerank="$rerank" -v max="$MAX_GPU_MEM_UTIL_SUM" \
    'BEGIN { exit !(main + embed + rerank <= max + 0.000001) }' ||
    die "сумма долей видеопамяти больше ${MAX_GPU_MEM_UTIL_SUM}: основная модель ${main}, эмбеддинги ${embed}, реранкер ${rerank}" \
      "снизить долю основной модели: добавить к команде GPU_MEM_UTIL=0.81 (install.sh --gpu-mem-util 0.81; docs/design.md §3.9)"
}

prepare_env() {
  if [[ ! -f "$ENV_FILE" ]]; then
    install -m 600 "$ENV_EXAMPLE" "$ENV_FILE"
    log "создан ${ENV_FILE} из .env.example"
  fi
  chmod 600 "$ENV_FILE"

  local image_tag
  image_tag="$(commit_tag "$REPO_DIR")"
  resolve_stage

  # Аргументы важнее .env; в файл пишем только после всех проверок: отказ не меняет
  # .env, и повторный запуск сохраняет для отката тот же прежний файл.
  hostname="${arg_hostname:-$(env_file_value LLM_HOSTNAME)}"
  tls_mode="${arg_tls:-$(env_file_value TLS_MODE)}"

  # DNS-имя и режим TLS нужны только Caddy, то есть на этапе gateway.
  if [[ "$gateway_enabled" -eq 1 ]]; then
    [[ "$hostname" =~ $HOSTNAME_RE ]] ||
      die "LLM_HOSTNAME «${hostname}» не задан или не похож на DNS-имя либо IP-адрес" \
        "указать --hostname llm.<корп.домен> (до привязки домена — IP-адрес ВМ)"
    [[ "$tls_mode" == "corp" || "$tls_mode" == "internal" ]] ||
      die "TLS_MODE «${tls_mode}»: ожидается corp или internal" \
        "указать --tls corp (есть сертификат корпоративного CA) или --tls internal (docs/design.md §3.4)"
  fi
  # Ключ выдаёт Bifrost, сгенерировать его нельзя; без него портал не обратится к модели.
  if [[ "$portal_enabled" -eq 1 && -z "$(env_file_value PORTAL_LLM_API_KEY)" ]]; then
    die "PORTAL_LLM_API_KEY в ${ENV_FILE} пуст" \
      "выдать ключ: make key NAME=portal RERANK=1; вписать его (sk-bf-...) в PORTAL_LLM_API_KEY в ${ENV_FILE} и повторить"
  fi
  check_gpu_shares

  save_rollback_env "$image_tag"
  add_missing_variables
  set_env_value LLM_HOSTNAME "$hostname"
  set_env_value TLS_MODE "$tls_mode"
  set_env_value COMPOSE_PROFILES "$profiles"
  [[ "$portal_enabled" -eq 0 ]] || set_env_value SITE_MODE portal
  [[ -z "$arg_gpu_mem_util" ]] || set_env_value GPU_MEM_UTIL "$arg_gpu_mem_util"
  # Образы портала соответствуют коду клона: при включённом портале тег следует за
  # коммитом. Без портала тег нужен только для интерполяции compose.
  if [[ "$portal_enabled" -eq 1 || -z "$(env_file_value PORTAL_IMAGE_TAG)" ]]; then
    set_env_value PORTAL_IMAGE_TAG "$image_tag"
  fi

  if [[ -z "$(env_file_value BIFROST_ADMIN_USERNAME)" ]]; then
    set_env_value BIFROST_ADMIN_USERNAME "$DEFAULT_ADMIN_USERNAME"
  fi
  ensure_secret VLLM_API_KEY random_hex
  ensure_secret BIFROST_ADMIN_PASSWORD random_hex
  ensure_secret BIFROST_ENCRYPTION_KEY random_base64
  ensure_secret GRAFANA_ADMIN_PASSWORD random_hex
  # На любом этапе: compose требует эти переменные и при выключенном профиле portal.
  ensure_secret PORTAL_DB_PASSWORD random_hex
  ensure_secret QDRANT_API_KEY random_hex
  ensure_secret PORTAL_SECRET_KEY random_base64
  # Пароли песочницы SQL — отдельные от пароля базы портала и друг от друга
  # (docs/portal-design.md §7, §8.1).
  ensure_secret SQL_SANDBOX_PASSWORD random_hex
  ensure_secret SQL_SANDBOX_CONTROL_PASSWORD random_hex
}

check_corp_certificate() {
  local cert="${CERTS_DIR}/fullchain.pem" key="${CERTS_DIR}/privkey.pem"
  local fix="положить сертификат корпоративного CA в ${cert} и ключ в ${key}, либо установить с --tls internal"
  local renew="запросить перевыпуск у DevOps (docs/devops-request.md §1.2)"
  # Заданный ACME_EMAIL значит, что сертификат — от Let's Encrypt (scripts/cert.sh).
  if [[ -n "$(env_file_value ACME_EMAIL)" ]]; then
    fix="выпустить сертификат Let's Encrypt: make cert; затем повторить этап"
    renew="продлить: make cert-renew; почему не сработал таймер — journalctl -u llm-cert-renew"
  fi
  [[ -f "$cert" ]] || die "нет файла ${cert}" "$fix"
  [[ -f "$key" ]] || die "нет файла ${key}" "$fix"
  chmod 600 "$key"

  openssl x509 -in "$cert" -noout >/dev/null 2>&1 ||
    die "${cert} не является PEM-сертификатом" "$fix"
  openssl x509 -in "$cert" -noout -checkend 0 >/dev/null ||
    die "сертификат ${cert} просрочен" "$renew"
  if ! openssl x509 -in "$cert" -noout -checkend "$((CERT_WARN_DAYS * 86400))" >/dev/null; then
    log "ВНИМАНИЕ: сертификат истекает менее чем через ${CERT_WARN_DAYS} дней; ${renew}"
  fi
  [[ "$(openssl x509 -in "$cert" -noout -checkhost "$hostname")" == *"does match"* ]] ||
    die "сертификат ${cert} выписан не на ${hostname}" "запросить сертификат с SAN = ${hostname}"
  [[ "$(openssl x509 -in "$cert" -noout -pubkey)" == "$(openssl pkey -in "$key" -pubout 2>/dev/null)" ]] ||
    die "ключ ${key} не соответствует сертификату ${cert}" "проверить, что файлы из одной пары"
  log "сертификат ${cert}: срок, имя и ключ в порядке"
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
  # Образы портала собираются здесь же: в реестре их нет, и запрашивать их там незачем.
  compose pull --ignore-buildable
}

# Подготовка релиза, до сборки новых образов: образы работающего портала получают тег
# коммита рабочего клона. Это путь отката (docs/portal-design.md §8.2): без тега новая
# сборка оставила бы от них только безымянные слои.
tag_running_portal_images() {
  local live_compose=(docker compose -f "${live_dir}/deploy/docker-compose.yml")
  local live_tag live_services service container image tagged=()
  live_tag="$(commit_tag "$live_dir")"
  [[ "$live_tag" != "$(env_file_value PORTAL_IMAGE_TAG)" ]] ||
    die "релизный клон на том же коммите ${live_tag}, что и рабочий" "переключить этот клон на релизный коммит: git checkout <коммит>"
  live_services="$("${live_compose[@]}" config --services)" ||
    die "docker compose не читает конфигурацию рабочего клона ${live_dir}" "проверить путь --prepare-from и ${live_dir}/deploy/.env"
  for service in "${PORTAL_BUILD_SERVICES[@]}"; do
    # Сервиса может не быть в прежнем релизе (или портал ещё не включён).
    grep -qx "$service" <<<"$live_services" || continue
    container="$("${live_compose[@]}" ps -a -q "$service" | head -n 1)"
    [[ -n "$container" ]] ||
      die "у сервиса ${service} рабочего стека нет контейнера: его образ не определить" "запустить рабочий стек (в ${live_dir}: make up) и повторить подготовку"
    image="$(docker inspect --format '{{.Image}}' "$container")"
    if ! { docker tag "$image" "${IMAGE_PREFIX}${service}:${live_tag}" &&
      docker image inspect "${IMAGE_PREFIX}${service}:${live_tag}" >/dev/null; }; then
      die "не удалось пометить образ сервиса ${service} тегом ${live_tag}" "docker ps -a; docker image ls"
    fi
    tagged+=("${IMAGE_PREFIX}${service}:${live_tag}")
  done
  if [[ "${#tagged[@]}" -gt 0 ]]; then
    log "нынешние образы портала помечены (путь отката): ${tagged[*]}"
  else
    log "в рабочем стеке нет сервисов портала: помечать нечего"
  fi
}

# Образы с тегом этого коммита уже собраны (подготовка релиза), и код портала в клоне
# не отличается от коммита — иначе образ под тем же тегом не соответствовал бы коду.
portal_images_ready() {
  local tag service
  tag="$(env_file_value PORTAL_IMAGE_TAG)"
  [[ -z "$(repo_git "$REPO_DIR" status --porcelain -- portal deploy/sql-sandbox)" ]] || return 1
  for service in "${PORTAL_BUILD_SERVICES[@]}"; do
    docker image inspect "${IMAGE_PREFIX}${service}:${tag}" >/dev/null 2>&1 || return 1
  done
}

build_portal_images() {
  [[ "$portal_enabled" -eq 1 ]] || return 0
  if portal_images_ready; then
    log "образы портала с тегом $(env_file_value PORTAL_IMAGE_TAG) уже собраны: сборка пропущена"
    return 0
  fi
  log "сборка образов портала: ${PORTAL_BUILD_SERVICES[*]} (первая сборка — несколько минут)"
  compose build "${PORTAL_BUILD_SERVICES[@]}" ||
    die "не удалось собрать образы портала" \
      "проверить доступ к Docker Hub, PyPI и npm; повторить: cd ${DEPLOY_DIR} && docker compose build ${PORTAL_BUILD_SERVICES[*]}"
}

preload_model() {
  local model_id="$1"
  log "предзагрузка весов ${model_id} (повторный запуск докачивает только недостающее)"
  compose run --rm --no-deps -e HF_HUB_OFFLINE=0 --entrypoint hf vllm download "$model_id"
}

# Веса модели речи — в том asr-models: воркер портала читает их оттуда и без них не
# стартует (docs/portal-design.md §3.3). Загрузка — утилитой hf из образа vLLM: он уже
# есть на ВМ. Файлы создаёт root с правами 644, воркеру (uid 10001) их достаточно.
preload_speech_models() {
  [[ "$portal_enabled" -eq 1 ]] || return 0
  local image entry dir repo revision files
  image="$(compose config --images | grep -m 1 '^vllm/')" ||
    die "в compose не найден образ vLLM: нечем загрузить веса модели речи" "cd ${DEPLOY_DIR} && docker compose config --images"
  # Метки compose: том создаётся раньше первого запуска портала, и без них compose
  # предупреждал бы, что том создан не им.
  docker volume inspect "$ASR_VOLUME" >/dev/null 2>&1 ||
    docker volume create --label "com.docker.compose.project=${COMPOSE_PROJECT}" \
      --label "com.docker.compose.volume=${ASR_VOLUME#"${COMPOSE_PROJECT}"_}" "$ASR_VOLUME" >/dev/null
  for entry in "${ASR_MODELS[@]}"; do
    read -r dir repo revision files <<<"$entry"
    log "предзагрузка весов ${repo} в том ${ASR_VOLUME} (${dir})"
    # shellcheck disable=SC2086  # список файлов — отдельными аргументами
    docker run --rm -e HF_HUB_OFFLINE=0 -v "${ASR_VOLUME}:/models/asr" --entrypoint hf "$image" \
      download "$repo" $files --revision "$revision" --local-dir "/models/asr/${dir}" ||
      die "не удалось загрузить веса ${repo}" "проверить доступ к huggingface.co и *.hf.co (docs/devops-request.md §1.4) и повторить"
  done
}

preload_models() {
  preload_model "$(env_file_value MODEL_ID)"
  if [[ ",${profiles}," == *",embeddings,"* ]]; then
    preload_model "$(env_file_value EMBED_MODEL_ID)"
  fi
  if [[ ",${profiles}," == *",rerank,"* ]]; then
    preload_model "$(env_file_value RERANK_MODEL_ID)"
  fi
  preload_speech_models
}

start_stack() {
  log "systemd-юнит llm-stack (WorkingDirectory=${DEPLOY_DIR})"
  sed "s#^WorkingDirectory=.*#WorkingDirectory=${DEPLOY_DIR}#" "$UNIT_SOURCE" >"$UNIT_TARGET"
  systemctl daemon-reload
  systemctl enable llm-stack

  log "запуск: vLLM загружает модель несколько минут; логи — cd ${DEPLOY_DIR} && docker compose logs -f vllm"
  # Сначала модели (docs/portal-design.md §8.2): portal-api от vLLM не зависит и при общем
  # запуске применил бы миграции раньше, чем выяснится, что модели с новыми долями
  # видеопамяти не поднялись. До этого места откат не требует восстановления данных.
  local models=(vllm)
  [[ ",${profiles}," != *",embeddings,"* ]] || models+=(vllm-embed)
  [[ ",${profiles}," != *",rerank,"* ]] || models+=(vllm-rerank)
  bash "${SCRIPT_DIR}/stack_up.sh" --wait "${models[@]}" ||
    die "модели не запустились: ${models[*]}" "если выше сказано о незавершённом восстановлении — повторить make restore FROM=<каталог копии>; иначе cd ${DEPLOY_DIR} && docker compose logs ${models[*]}; nvidia-smi; доли видеопамяти — docs/design.md §3.9. Сервисы портала не перезапускались, их данные не затронуты"
  # start — первый запуск через юнит; up --wait — применить изменения при повторной
  # установке (у активного oneshot-юнита start ничего не делает) и дождаться healthy.
  # Через stack_up.sh, как и юнит: при следах прерванного восстановления пишущие сервисы
  # не запускаются (docs/portal-design.md §8).
  systemctl start llm-stack ||
    die "не удалось запустить стек" "sudo journalctl -u llm-stack; cd ${DEPLOY_DIR} && docker compose ps"
  bash "${SCRIPT_DIR}/stack_up.sh" --wait ||
    die "стек запущен не полностью" "если выше сказано о незавершённом восстановлении — повторить make restore FROM=<каталог копии>; иначе cd ${DEPLOY_DIR} && docker compose ps и logs <сервис>; docs/runbook.md §5"
  apply_mounted_configs
}

# config.json и Caddyfile смонтированы в контейнеры отдельными файлами: после git pull у
# них новый inode, а up -d не пересоздаёт контейнер из-за изменившегося содержимого —
# Bifrost и Caddy работали бы с конфигурацией прежнего релиза (Caddy читает Caddyfile
# только при старте, а caddy reload в контейнере увидел бы прежний файл). Том caddy-data
# с корнем CA сохраняется. Вызывается после успешного запуска всего стека.
apply_mounted_configs() {
  [[ "$gateway_enabled" -eq 1 ]] || return 0
  log "пересоздание Bifrost и Caddy: применяются текущие deploy/bifrost/config.json и deploy/caddy/Caddyfile (сайт недоступен несколько секунд)"
  compose up -d --force-recreate --no-deps --wait bifrost caddy ||
    die "Bifrost или Caddy не запустились с текущей конфигурацией" "cd ${DEPLOY_DIR} && docker compose logs bifrost caddy"
}

# Файл юнита → каталог systemd с фактическим путём к deploy/ (у таймера такой строки нет).
install_unit() {
  sed "s#^WorkingDirectory=.*#WorkingDirectory=${DEPLOY_DIR}#" "$1" >"${SYSTEMD_DIR}/$(basename "$1")"
}

# Таймер имеет смысл, только если сертификат выпущен lego (make cert) и продлевать его
# можно без человека — через API DNS. Ставится здесь, а не в cert.sh: юниты systemd с
# путём к клону устанавливает этот скрипт, а первый выпуск идёт до появления Caddy.
install_cert_renewal() {
  [[ "$gateway_enabled" -eq 1 && "$tls_mode" == "corp" ]] || return 0
  [[ -f "${LEGO_CERTS_DIR}/${hostname}.crt" ]] || return 0
  if [[ -z "$(env_file_value TIMEWEBCLOUD_AUTH_TOKEN)" ]]; then
    log "ВНИМАНИЕ: TIMEWEBCLOUD_AUTH_TOKEN пуст — автопродление сертификата не включено. Продлевать вручную не позже чем через 60 дней: make cert-renew MANUAL=1"
    return 0
  fi
  log "таймер ${RENEW_TIMER}: ежедневная проверка срока сертификата (make cert-renew)"
  local unit
  for unit in "${RENEW_UNITS[@]}"; do
    install_unit "$unit"
  done
  systemctl daemon-reload
  systemctl enable --now "$RENEW_TIMER" ||
    die "не удалось включить ${RENEW_TIMER}" "sudo systemctl status ${RENEW_TIMER}; до исправления продлевать вручную: make cert-renew"
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

# Этап portal: кроме путей API сайт отдаёт портал; админ-API Bifrost и /metrics по-прежнему
# закрыты (docs/portal-design.md §3). Аргументы — общие параметры curl.
verify_portal() {
  local hint="docs/runbook.md §5; cd ${DEPLOY_DIR} && docker compose logs caddy portal-web portal-api"
  local root="https://${hostname}"

  expect_http "страница входа портала открывается" "200" "${root}/" "$hint" "$@" </dev/null
  expect_http "портал без сессии отвечает 401" "401" "${root}/api/auth/session" "$hint" "$@" </dev/null
  expect_http "админ-API Bifrost через 443 закрыт" "404" \
    "${root}/api/governance/virtual-keys" "$hint" "$@" </dev/null
  expect_http "/metrics через 443 закрыт" "404" "${root}/metrics" "$hint" "$@" </dev/null
}

# Этап gateway: наружу через 443 видны только четыре пути API под /v1 и только с ключом
# Bifrost; на остальных портах Caddy — тот же сайт.
verify_gateway() {
  local hint="docs/runbook.md §5; cd ${DEPLOY_DIR} && docker compose logs caddy bifrost"
  local port route
  local tls_args=(--cacert "$ROOT_CERT_FILE")
  # Корень корпоративного CA на ВМ может быть не установлен; сертификат уже проверен выше.
  [[ "$tls_mode" == "internal" ]] || tls_args=(--insecure)
  local common=("${tls_args[@]}" --resolve "${hostname}:443:127.0.0.1")
  for port in "${CADDY_EXTRA_PORTS[@]}"; do
    common+=(--resolve "${hostname}:${port}:127.0.0.1")
  done

  # Именно inference-запрос: на него действует enforce_auth_on_inference Bifrost.
  expect_http "запрос к модели без ключа отклоняется" "401|403" \
    "https://${hostname}/v1/chat/completions" "$hint" "${common[@]}" \
    -X POST -H "Content-Type: application/json" -d "$MINIMAL_CHAT_BODY" </dev/null
  for route in "${V1_CLOSED_ROUTES[@]}"; do
    expect_http "${route} через 443 закрыт" "404" "https://${hostname}${route#* }" \
      "Caddy должен проксировать под /v1 только четыре пути API (deploy/caddy/Caddyfile); ${hint}" \
      "${common[@]}" -X "${route%% *}" </dev/null
  done
  if [[ "$portal_enabled" -eq 1 ]]; then
    verify_portal "${common[@]}"
  else
    expect_http "/ через 443 закрыт" "404" "https://${hostname}/" "$hint" "${common[@]}" </dev/null
  fi
  for port in "${CADDY_EXTRA_PORTS[@]}"; do
    expect_http "порт ${port}: запрос к модели без ключа отклоняется" "401|403" \
      "https://${hostname}:${port}/v1/chat/completions" \
      "sudo ss -ltnp | grep ':${port} ' — порт должен принадлежать Caddy (docker-proxy); ${hint}" "${common[@]}" \
      -X POST -H "Content-Type: application/json" -d "$MINIMAL_CHAT_BODY" </dev/null
  done
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
                   тот же сайт на портах ${CADDY_EXTRA_PORTS[*]}; после проброса порта из
                   интернета — https://${hostname}:18443/v1 (docs/portal-design.md §3.2)
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
  2. UI Bifrost и Grafana с рабочей станции: ssh -L ${BIFROST_ADMIN_PORT}:127.0.0.1:${BIFROST_ADMIN_PORT} -L 3000:127.0.0.1:3000 <админ>@<вм>
EOF
}

print_portal_summary() {
  local ca_arg=" CA=<PEM корневого CA, если он не публичный>"
  [[ "$tls_mode" == "corp" ]] || ca_arg=" CA=${ROOT_CERT_FILE}"
  cat <<EOF

===== Этап 3 завершён: портал развёрнут =====
Портал:            https://${hostname}/
API для клиентов:  https://${hostname}/v1   model="default"
                   тот же сайт на портах ${CADDY_EXTRA_PORTS[*]}; после проброса порта из
                   интернета — https://${hostname}:18443/ (docs/portal-design.md §3.2)
Секреты:           ${ENV_FILE} (root, 600)

ВАЖНО: сохраните копию PORTAL_SECRET_KEY из ${ENV_FILE} вне ВМ, в хранилище секретов
(вместе с BIFROST_ENCRYPTION_KEY). Им зашифрованы секреты второго фактора: без него
после восстановления из бэкапа второй фактор всем придётся настраивать заново.
EOF
  if [[ "$tls_mode" == "internal" ]]; then
    cat <<EOF

TLS: собственный CA Caddy. Чтобы браузер не предупреждал о сертификате, установите
${ROOT_CERT_FILE} как доверенный корневой на ПК сотрудников (docs/vpn-launch.md).
EOF
  fi
  cat <<EOF

Дальше (docs/vpn-launch.md):
  1. Первый администратор (временный пароль выводится один раз — сохраните его):
       make portal-admin LOGIN=<логин> NAME="<ФИО>"
  2. Проверка портала, закрытых маршрутов через 443 и сервисов (песочница SQL, реранкер):
       make smoke-portal${ca_arg}
  3. Резервная копия (база, файлы, векторы, ключи Bifrost):
       make backup DIR=<каталог вне репозитория>
EOF
}

print_summary() {
  if [[ "$portal_enabled" -eq 1 ]]; then
    print_portal_summary
  elif [[ "$gateway_enabled" -eq 1 ]]; then
    print_gateway_summary
  else
    print_model_summary
  fi
}

# Подготовка релиза заканчивается здесь: стек не перезапускается.
print_prepare_summary() {
  local tag live_tag profile_args=""
  tag="$(env_file_value PORTAL_IMAGE_TAG)"
  live_tag="$(commit_tag "$live_dir")"
  [[ -z "$(without_stage_profiles "$profiles")" ]] || profile_args=" PROFILES=$(without_stage_profiles "$profiles")"
  cat <<EOF

===== Подготовка релиза завершена: работающий стек и рабочий клон не изменены =====
Образы релиза:   ${IMAGE_PREFIX}<сервис>:${tag}  (${PORTAL_BUILD_SERVICES[*]})
Путь отката:     образы работающего портала помечены тегом ${live_tag} — коммитом
                 рабочего клона.
                 До закрытия релиза образы не чистить: docker image prune не выполнять.

Этот клон (${REPO_DIR}) до закрытия релиза не удалять и не переключать на другой
коммит: копия перед релизом и откат запускаются из него — в прежнем коммите рабочего
клона целей release-* нет.

Дальше (docs/portal-design.md §8.2):
  1. Здесь же — репетиция миграций на копии рабочей базы:
       make release-rehearse LIVE=${live_dir}
  2. В окне обслуживания.
     Здесь же:
       make release-backup LIVE=${live_dir} DIR=<каталог вне диска />
     В рабочем клоне ${live_dir}:
       git checkout ${tag}
       make portal${profile_args}${arg_gpu_mem_util:+ GPU_MEM_UTIL=${arg_gpu_mem_util}}
       make keys && make key-rerank ID=<id ключа portal>     (с профилем rerank)
       make smoke KEY=sk-bf-... && make smoke-portal
  3. Откат. В рабочем клоне ${live_dir}:
       git checkout ${live_tag}
     Здесь же:
       make release-rollback LIVE=${live_dir}
     (если новый portal-api уже стартовал — добавить FROM=<каталог копии>).
EOF
}

main() {
  parse_args "$@"
  check_system
  run_preflight
  if [[ "$prepare_mode" -eq 1 ]]; then
    # Сертификат лежит в рабочем клоне и нужен только запущенному Caddy: не проверяется.
    copy_live_env
    prepare_env
    pull_images
    tag_running_portal_images
    build_portal_images
    preload_models
    print_prepare_summary
    return 0
  fi
  prepare_env
  prepare_tls
  pull_images
  build_portal_images
  preload_models
  start_stack
  install_cert_renewal
  export_root_certificate
  verify_endpoint
  print_summary
}

# При source (тесты) функции только определяются.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
