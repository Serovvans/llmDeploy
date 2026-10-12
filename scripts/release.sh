#!/usr/bin/env bash
# Шаги релиза портала на работающей ВМ (docs/portal-design.md §8.2). Обёртки — make
# release-prepare, release-rehearse, release-backup, release-rollback.
#
# Сценарий всегда запускается из отдельного клона релизного коммита (релизный клон), а
# рабочий клон получает аргументом: в прежнем коммите рабочего клона ни этого сценария,
# ни целей release-* нет. Релизный клон не удаляется и не переключается до закрытия
# релиза — из него же выполняется откат.
#
# Порядок релиза:
#   до окна, из релизного клона (рабочий клон не меняется):
#     sudo bash scripts/install.sh --prepare-from РАБОЧИЙ_КЛОН [--profiles ...]
#         место на диске, образы, веса; нынешние образы портала получают тег отката;
#     sudo bash scripts/release.sh rehearse РАБОЧИЙ_КЛОН
#   в окне:
#     из релизного клона:  sudo bash scripts/release.sh backup РАБОЧИЙ_КЛОН КАТАЛОГ
#     в рабочем клоне:     git checkout <релизный коммит>
#                          sudo bash scripts/install.sh --stage portal --skip-preflight [--profiles ...]
#   при сбое:
#     в рабочем клоне:     git checkout <прежний коммит>
#     из релизного клона:  sudo bash scripts/release.sh rollback РАБОЧИЙ_КЛОН [КАТАЛОГ_КОПИИ]
#
#   rehearse РАБОЧИЙ_КЛОН
#       Репетиция миграций. Дамп рабочей базы загружается во временный PostgreSQL —
#       отдельный контейнер в своей сети без выхода наружу, — и на нём выполняется
#       portal migrate из образа релиза. Рабочая база только читается (pg_dump), данные
#       ВМ не покидают; временный контейнер и его том удаляются при любом исходе. На
#       время репетиции нужно свободное место размером с базу.
#
#   backup РАБОЧИЙ_КЛОН КАТАЛОГ
#       Резервная копия перед релизом (scripts/backup.sh create рабочего клона). Каталог
#       обязан лежать не на диске /: копия не должна занимать место, нужное сервису, и
#       пропасть вместе с диском ВМ.
#
#   rollback РАБОЧИЙ_КЛОН [КАТАЛОГ_КОПИИ]
#       Откат релиза. Рабочий клон уже возвращён на прежний коммит. Возвращает его .env,
#       сохранённый установкой перед релизом (deploy/rollback/ рабочего клона, копия для
#       этого коммита: профили, доля видеопамяти), останавливает стек целиком — вместе
#       с сервисами, которых в прежнем релизе нет, — и поднимает его из образов с тегом
#       прежнего коммита, без сборки. Копию восстанавливают и стек запускают backup.sh и
#       stack_up.sh рабочего клона, то есть прежнего релиза. С КАТАЛОГОМ_КОПИИ перед
#       запуском портала данные восстанавливаются из копии: это обязательно, если новый
#       portal-api успел стартовать, — миграции уже применены, обратных нет. Всё
#       созданное после копии при этом теряется. Копия проверяется (завершена,
#       контрольные суммы), и подтверждение (yes) запрашивается до остановки стека.
set -euo pipefail

# Общие функции и константы: тег коммита, список образов портала. Пути install.sh
# (deploy/.env, compose) относятся к релизному клону; рабочий клон задаётся аргументом.
# shellcheck source-path=SCRIPTDIR
source "$(dirname "${BASH_SOURCE[0]}")/install.sh"

readonly REHEARSAL_NETWORK="${COMPOSE_PROJECT}-rehearsal"
readonly REHEARSAL_DB="${COMPOSE_PROJECT}-rehearsal-db"
readonly REHEARSAL_READY_TIMEOUT_S=60
readonly DB_SERVICE="portal-db"
readonly DB_NAME="portal"
readonly DB_USER="portal"
# Признаки завершённой копии — те же, что проверяет backup.sh (verify_copy).
readonly COPY_PARTIAL_SUFFIX=".partial"
readonly COPY_CHECKSUMS="SHA256SUMS"
readonly COPY_DB_DUMP="portal-db.dump"

usage() {
  sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed -e '$d' -e 's/^# \{0,1\}//'
}

cleanup_rehearsal() {
  docker rm -f -v "$REHEARSAL_DB" >/dev/null 2>&1 || true
  docker network rm "$REHEARSAL_NETWORK" >/dev/null 2>&1 || true
}

wait_rehearsal_db() {
  local waited=0
  # По TCP: во время инициализации временный сервер слушает только сокет.
  until docker exec "$REHEARSAL_DB" pg_isready -q -h 127.0.0.1 -U "$DB_USER" -d "$DB_NAME"; do
    waited=$((waited + 1))
    [[ "$waited" -lt "$REHEARSAL_READY_TIMEOUT_S" ]] ||
      die "временная база не поднялась за ${REHEARSAL_READY_TIMEOUT_S} с" "docker logs ${REHEARSAL_DB} (до завершения команды); проверить свободное место: df -h"
    sleep 1
  done
}

cmd_rehearse() {
  local live_dir="$1"
  local live_compose=(docker compose -f "${live_dir}/deploy/docker-compose.yml")
  local tag image db_container db_image
  tag="$(commit_tag "$REPO_DIR")"
  image="${IMAGE_PREFIX}portal-api:${tag}"
  docker image inspect "$image" >/dev/null 2>&1 ||
    die "нет образа ${image}" "сначала подготовка релиза из этого клона: make release-prepare LIVE=${live_dir}"
  db_container="$("${live_compose[@]}" ps -q "$DB_SERVICE")" ||
    die "docker compose не читает конфигурацию рабочего клона ${live_dir}" "указать каталог рабочего клона, из которого запущен сервис"
  [[ -n "$db_container" ]] ||
    die "в рабочем стеке не запущен ${DB_SERVICE}" "в ${live_dir}: make ps; make up"
  # Тот же образ PostgreSQL, что у рабочей базы.
  db_image="$(docker inspect --format '{{.Config.Image}}' "$db_container")"

  # Пароль временной базы — случайный и только в окружении: в аргументы не попадает.
  POSTGRES_PASSWORD="$(random_hex)"
  export POSTGRES_PASSWORD
  export PORTAL_DB_PASSWORD="$POSTGRES_PASSWORD"
  cleanup_rehearsal
  trap cleanup_rehearsal EXIT
  log "временная база: ${db_image}, сеть ${REHEARSAL_NETWORK} без выхода наружу"
  docker network create --internal "$REHEARSAL_NETWORK" >/dev/null
  # Псевдоним portal-db: под этим именем образ портала ищет базу.
  docker run -d --name "$REHEARSAL_DB" --network "$REHEARSAL_NETWORK" --network-alias "$DB_SERVICE" \
    -e POSTGRES_USER="$DB_USER" -e POSTGRES_DB="$DB_NAME" -e POSTGRES_PASSWORD "$db_image" >/dev/null
  wait_rehearsal_db

  log "дамп рабочей базы → временная база"
  "${live_compose[@]}" exec -T "$DB_SERVICE" pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom </dev/null |
    docker exec -i "$REHEARSAL_DB" pg_restore -U "$DB_USER" -d "$DB_NAME" --exit-on-error ||
    die "дамп рабочей базы не загрузился во временную" "в ${live_dir}: make logs SERVICE=${DB_SERVICE}; проверить свободное место: df -h"

  log "миграции релиза ${tag} на копии базы"
  SECONDS=0
  docker run --rm --network "$REHEARSAL_NETWORK" -e PORTAL_DB_PASSWORD "$image" portal migrate ||
    die "миграции релиза ${tag} не прошли на копии рабочей базы" "релиз не выкатывать; сообщение об ошибке выше передать разработчикам. Рабочая база не менялась"
  log "репетиция миграций прошла за ${SECONDS} с; временная база удаляется"
}

# Рабочий клон — каталог, из которого запущен сервис; его коммит может быть прежним.
check_live_dir() {
  [[ -f "$1/deploy/docker-compose.yml" && -f "$1/scripts/backup.sh" && -f "$1/scripts/stack_up.sh" ]] ||
    die "$1 не похож на рабочий клон: нет deploy/docker-compose.yml, scripts/backup.sh или scripts/stack_up.sh" "указать каталог клона, из которого запущен сервис: systemctl cat llm-stack, строка WorkingDirectory без /deploy"
}

cmd_backup() {
  local live_dir="$1" dir="$2"
  check_live_dir "$live_dir"
  [[ -d "$dir" ]] || die "нет каталога ${dir}" "создать каталог на диске, отличном от /, например на подключённом томе или сетевом ресурсе"
  [[ "$(df -P "$dir" | awk 'NR == 2 { print $1 }')" != "$(df -P / | awk 'NR == 2 { print $1 }')" ]] ||
    die "каталог ${dir} лежит на диске /" "указать каталог на другом диске или сетевом ресурсе (docs/portal-design.md §3.4, §8.2): df -h ${dir}"
  bash "${live_dir}/scripts/backup.sh" create "$dir"
}

# Прежний .env с тегом образов прежнего коммита → .env рабочего клона, права 600.
restore_live_env() {
  local saved_env="$1" live_env="$2" tag="$3" tmp
  tmp="$(mktemp "${live_env}.XXXXXX")"
  chmod 600 "$tmp"
  awk '!/^PORTAL_IMAGE_TAG=/' "$saved_env" >"$tmp"
  echo "PORTAL_IMAGE_TAG=${tag}" >>"$tmp"
  mv "$tmp" "$live_env"
}

# sha256sum есть на ВМ (coreutils); shasum — на macOS, где идут локальные проверки.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  else
    shasum -a 256 "$@"
  fi
}

# До остановки стека: полную проверку копии backup.sh рабочего клона делает уже при
# остановленном стеке, и негодная копия оставила бы сервис выключенным.
check_copy() {
  local source="${1%/}"
  local hint="указать каталог завершённой копии (llm-backup-...), которую создал make release-backup; стек не останавливался"
  [[ -d "$source" ]] || die "нет каталога ${source}" "$hint"
  [[ "$source" != *"$COPY_PARTIAL_SUFFIX" ]] || die "${source} — незавершённая копия (${COPY_PARTIAL_SUFFIX})" "$hint"
  [[ -f "${source}/${COPY_CHECKSUMS}" && -s "${source}/${COPY_DB_DUMP}" ]] ||
    die "${source} не похож на завершённую копию: нет ${COPY_CHECKSUMS} или ${COPY_DB_DUMP}" "$hint"
  log "проверка контрольных сумм копии ${source}"
  (cd "$source" && sha256 -c "$COPY_CHECKSUMS" >/dev/null 2>&1) ||
    die "контрольные суммы копии ${source} не совпадают" "взять другую копию: эта повреждена при переносе; стек не останавливался"
}

confirm_rollback() {
  local source="$1" answer=""
  echo "Стек будет остановлен, данные сервиса — ЗАМЕНЕНЫ содержимым ${source}: всё созданное после копии теряется."
  # Конец ввода без ответа — тоже отказ, а не молчаливый выход по set -e.
  read -r -p "Продолжить? Введите yes: " answer || true
  [[ "$answer" == "yes" ]] || die "откат отменён: стек не останавливался, .env не менялся" "для продолжения повторить команду и ввести yes"
}

cmd_rollback() {
  local live_dir="$1" source="$2" tag saved_env services service
  check_live_dir "$live_dir"
  local live_compose=(docker compose -f "${live_dir}/deploy/docker-compose.yml")
  tag="$(commit_tag "$live_dir")"
  saved_env="$(rollback_env_path "$live_dir" "$tag")"
  # В .env версии до 0.5 тега образов нет: её копия лежит без подкаталога.
  [[ -f "$saved_env" ]] || saved_env="$(rollback_env_path "$live_dir" "")"
  [[ -f "$saved_env" ]] ||
    die "в ${live_dir}/deploy/rollback нет копии .env до релиза для коммита ${tag}" "проверить, что рабочий клон возвращён на коммит прежнего релиза (в ${live_dir}: git checkout <коммит>), и каталог рабочего клона: копию сохраняет установка релиза (make portal). Если установка не дошла до записи .env, он не менялся и откат не нужен"
  # Прежний .env подходит только к compose прежнего коммита: так ловится клон,
  # оставшийся на коммите неудачного релиза.
  services="$("${live_compose[@]}" --env-file "$saved_env" config --services 2>/dev/null)" ||
    die "прежний .env (${saved_env}) не подходит к docker-compose.yml коммита ${tag}" "вернуть рабочий клон на коммит прежнего релиза: в ${live_dir} git checkout <коммит> — и повторить"
  for service in "${PORTAL_BUILD_SERVICES[@]}"; do
    grep -qx "$service" <<<"$services" || continue
    docker image inspect "${IMAGE_PREFIX}${service}:${tag}" >/dev/null 2>&1 ||
      die "нет образа ${IMAGE_PREFIX}${service}:${tag}" "образы прежнего релиза помечает подготовка (make release-prepare); проверить коммит рабочего клона и docker image ls '${IMAGE_PREFIX}*'"
  done
  if [[ -n "$source" ]]; then
    check_copy "$source"
    confirm_rollback "$source"
  fi

  restore_live_env "$saved_env" "${live_dir}/deploy/.env" "$tag"
  for service in "${PORTAL_BUILD_SERVICES[@]}"; do
    grep -qx "$service" <<<"$services" || continue
    # Compose до 0.5 не задаёт имён образов и берёт <проект>-<сервис>:latest.
    docker tag "${IMAGE_PREFIX}${service}:${tag}" "${IMAGE_PREFIX}${service}:latest"
  done
  log "возвращён прежний .env; образы портала — с тегом ${tag}"

  log "остановка стека"
  systemctl stop llm-stack
  # Все профили и осиротевшие контейнеры: сервисов неудачного релиза (реранкер,
  # песочница SQL) в прежнем compose или в прежнем наборе профилей нет.
  "${live_compose[@]}" --profile '*' down --remove-orphans

  if [[ -n "$source" ]]; then
    log "восстановление данных из ${source}"
    "${live_compose[@]}" up -d --wait "$DB_SERVICE" ||
      die "сервис ${DB_SERVICE} не запустился" "в ${live_dir}: make logs SERVICE=${DB_SERVICE}; затем make restore FROM=${source} и make up"
    # Подтверждение уже получено выше; backup.sh спрашивает его со stdin ещё раз.
    bash "${live_dir}/scripts/backup.sh" restore "$source" <<<"yes" ||
      die "восстановление не выполнено: стек остановлен" "исправить причину из сообщения выше и повторить отсюда же: make release-rollback LIVE=${live_dir} FROM=${source}"
  fi

  bash "${live_dir}/scripts/stack_up.sh" --wait ||
    die "стек прежнего релиза запущен не полностью" "в ${live_dir}: make ps; make logs SERVICE=<сервис>. Если не стартует portal-api, а новый релиз успел применить миграции, — нужен откат с копией, отсюда же: make release-rollback LIVE=${live_dir} FROM=<каталог копии>"
  systemctl start llm-stack ||
    die "стек запущен, но юнит llm-stack не стартовал" "sudo journalctl -u llm-stack"
  log "откат завершён: работает релиз ${tag}. Проверка — в ${live_dir}: make ps; make smoke-portal"
}

main() {
  case "${1:-}" in
    -h | --help)
      usage
      exit 0
      ;;
    rehearse)
      [[ $# -eq 2 ]] || die "rehearse требует каталог рабочего клона" "bash scripts/release.sh --help"
      ;;
    backup)
      [[ $# -eq 3 ]] || die "backup требует каталог рабочего клона и каталог для копии" "bash scripts/release.sh --help"
      ;;
    rollback)
      [[ $# -eq 2 || $# -eq 3 ]] || die "rollback требует каталог рабочего клона и принимает каталог копии" "bash scripts/release.sh --help"
      ;;
    *) die "ожидается rehearse, backup или rollback" "bash scripts/release.sh --help" ;;
  esac
  command -v docker >/dev/null 2>&1 || die "не найден docker" "запустить на ВМ"
  "cmd_$1" "$2" "${3:-}"
}

# При source (тесты) функции только определяются.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
