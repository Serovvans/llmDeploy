#!/usr/bin/env bash
# Резервная копия данных сервиса и восстановление из неё (docs/portal-design.md §8,
# docs/design.md §8). Запуск на ВМ из клона репозитория; обёртки — make backup / restore.
#
#   sudo bash scripts/backup.sh create КАТАЛОГ
#       Создаёт КАТАЛОГ/llm-backup-ГГГГММДД-ЧЧММСС/ (права 700, файлы 600). Пока копия
#       не завершена (или если она оборвалась), каталог носит суффикс .partial.
#
#   sudo bash scripts/backup.sh restore КАТАЛОГ_КОПИИ
#       Заменяет данные сервиса содержимым копии. Спрашивает подтверждение.
#
# Состав копии — данные сервисов включённых профилей:
#   portal-db.dump     база портала: дамп pg_dump (согласованный снимок, а не копия
#                      каталога работающего сервера);
#   portal-files.tgz   volume portal-files — оригиналы документов и вложения;
#   qdrant-data.tgz    volume qdrant-data — векторы; без него база знаний
#                      восстанавливается переиндексацией (make portal-reindex-recreate);
#   bifrost-data.tgz   volume bifrost-data — ключи клиентов, лимиты, логи запросов;
#   SHA256SUMS         контрольные суммы файлов копии.
#
# На время копирования и восстановления останавливаются portal-api, portal-worker,
# qdrant и bifrost: иначе файлы, векторы и SQLite не соответствовали бы снимку базы.
# Портал и API в это время недоступны.
#
# Восстановление не трогает данные, пока копия не проверена и не распакована рядом:
#   1. до подтверждения — контрольные суммы, читаемость дампа и архивов;
#   2. дамп загружается во временную базу, архивы распаковываются во временный каталог
#      внутри своего тома — прежние данные при сбое остаются как были;
#   3. только затем база и тома переключаются на новое содержимое.
# Копия без SHA256SUMS и каталог .partial не принимаются.
#
# Прерванное восстановление оставляет следы: базы portal_restore / portal_previous и
# каталог .restore-new (с отметкой .restore-new.ready) в томах. Пока они есть, данные
# могут быть смешанными — часть из копии, часть прежние: сервисы запускать нельзя,
# create отказывает. Выход один — повторить restore: он доводит переключение до конца.
#
# В копию НЕ входят:
#   deploy/.env — секреты в открытом виде. PORTAL_SECRET_KEY и BIFROST_ENCRYPTION_KEY
#       хранятся отдельно, вне ВМ: без первого второй фактор всем придётся настраивать
#       заново, без второго база ключей Bifrost не читается;
#   volume caddy-data — приватный ключ корня CA (TLS_MODE=internal), docs/runbook.md §4.4;
#   volume hf-cache — веса моделей скачиваются заново.
# В самой копии — личные данные и переписка сотрудников: хранить как секрет.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
# COMPOSE_FILE и COMPOSE_PROJECT_NAME — стандартные переменные docker compose: ими
# скрипт направляют на стенд (portal/dev) или на отдельный проект для проверки копии.
readonly COMPOSE_FILE_PATH="${COMPOSE_FILE:-${SCRIPT_DIR}/../deploy/docker-compose.yml}"
readonly BACKUP_PREFIX="llm-backup-"
readonly PARTIAL_SUFFIX=".partial"
readonly CHECKSUMS="SHA256SUMS"
readonly DB_DUMP="portal-db.dump"
readonly DB_SERVICE="portal-db"
readonly DB_NAME="portal"
readonly DB_USER="portal"
# Временная база для загрузки дампа и прежняя база на время переключения.
readonly DB_STAGING="portal_restore"
readonly DB_PREVIOUS="portal_previous"
readonly PORTAL_VOLUMES=(portal-files qdrant-data)
readonly BIFROST_VOLUME="bifrost-data"
# Временный каталог внутри тома, куда распаковывается архив до переключения.
readonly VOLUME_STAGING=".restore-new"
# Отметка «архив распакован целиком»: без неё прежнее содержимое тома не удаляется.
readonly VOLUME_READY=".restore-new.ready"
readonly RETRY_HINT="повторить ту же команду восстановления (make restore FROM=<каталог копии>) — она доведёт его до конца; до этого сервисы не запускать (make up): база и тома могут не соответствовать друг другу"
# Все, кто пишет в копируемые данные.
readonly WRITERS=(portal-worker portal-api qdrant bifrost)
# tar запускается в образе этого сервиса: он закреплён в compose и уже есть на ВМ при
# любом наборе профилей с данными (gateway), поэтому копия и аварийное восстановление не
# зависят от доступа к реестру. Тома монтируются напрямую, от root.
readonly HELPER_SERVICE="caddy"

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
  docker compose -f "$COMPOSE_FILE_PATH" "$@"
}

has_service() {
  grep -qx "$1" <<<"$enabled_services"
}

# sha256sum есть на ВМ (coreutils); shasum — на macOS, где идут локальные проверки.
sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$@"
  else
    shasum -a 256 "$@"
  fi
}

# Читает из compose всё, что нужно до любых действий: имя проекта (префикс томов),
# сервисы включённых профилей, образ для tar. Заполняет список файлов копии.
load_project() {
  local config_error="запустить от root на ВМ; проверить deploy/.env"
  enabled_services="$(compose config --services)" ||
    die "docker compose не читает ${COMPOSE_FILE_PATH}" "$config_error"
  # Имя берётся из compose, а не из окружения: тома должны быть того же проекта, чьи
  # сервисы останавливаются.
  project="$(compose config | sed -n 's/^name: *//p' | head -n 1 | tr -d "\"'")"
  [[ -n "$project" ]] || die "не удалось определить имя проекта compose" "$config_error"

  writers=()
  local service
  for service in "${WRITERS[@]}"; do
    if has_service "$service"; then
      writers+=("$service")
    fi
  done
  [[ "${#writers[@]}" -gt 0 ]] ||
    die "во включённых профилях нет ни портала, ни Bifrost — копировать нечего" \
      "копия нужна после этапа 2 (make gateway) или 3 (make portal)"

  volumes=()
  copy_files=()
  if has_service "$DB_SERVICE"; then
    volumes+=("${PORTAL_VOLUMES[@]}")
    copy_files+=("$DB_DUMP")
  fi
  if has_service bifrost; then
    volumes+=("$BIFROST_VOLUME")
  fi
  copy_files+=("${volumes[@]/%/.tgz}")

  helper_image="$(compose config --images | grep -m 1 "^${HELPER_SERVICE}:" || true)"
  [[ -n "$helper_image" ]] ||
    die "в compose нет сервиса ${HELPER_SERVICE}: его образ нужен для tar" "проверить COMPOSE_PROFILES в deploy/.env (профиль gateway)"
}

volume_name() {
  printf '%s_%s' "$project" "$1"
}

# Всё, без чего операция оборвётся на середине, проверяется до остановки сервисов.
check_prerequisites() {
  docker image inspect "$helper_image" >/dev/null 2>&1 ||
    die "нет образа ${helper_image}" "cd deploy && docker compose pull ${HELPER_SERVICE}"
  local volume
  for volume in "${volumes[@]}"; do
    # Без этой проверки docker run молча создал бы пустой том с таким именем.
    docker volume inspect "$(volume_name "$volume")" >/dev/null 2>&1 ||
      die "нет тома $(volume_name "$volume")" "docker volume ls; сервис с этим томом должен быть хотя бы раз запущен"
  done
  if has_service "$DB_SERVICE"; then
    db_run pg_isready -q -U "$DB_USER" -d postgres ||
      die "сервис ${DB_SERVICE} не отвечает" "make ps; make up"
  fi
}

stop_writers() {
  log "остановка на время операции: ${writers[*]}"
  compose stop "${writers[@]}"
}

start_writers() {
  log "запуск: ${writers[*]}"
  compose start "${writers[@]}" ||
    die "не удалось запустить ${writers[*]}" "make ps; make logs SERVICE=<сервис>"
}

# Команда в контейнере базы, читающая файл копии со stdin.
db_exec() {
  compose exec -T "$DB_SERVICE" "$@"
}

# То же без ввода: иначе exec -T забрал бы stdin скрипта вместе с ответом оператора.
db_run() {
  db_exec "$@" </dev/null
}

# run_in_volume ТОМ РЕЖИМ КОМАНДА...: команда в образе-помощнике с томом в /vol.
# Точка монтирования, которой в образе нет: иначе при пустом томе Docker скопировал бы
# в него содержимое и владельца каталога из образа (у caddy есть свой /data).
run_in_volume() {
  local volume="$1" mode="$2"
  shift 2
  docker run --rm -i -v "$(volume_name "$volume"):/vol${mode}" "$helper_image" "$@"
}

# Дописывает префикс к каждой строке stdin.
prefix_lines() {
  local line
  while IFS= read -r line; do
    printf '%s%s\n' "$1" "$line"
  done
}

# Следы незавершённого восстановления, по строке на каждый; пусто — их нет.
restore_traces() {
  local volume found
  if has_service "$DB_SERVICE"; then
    found="$(db_run psql -At -U "$DB_USER" -d postgres -c \
      "SELECT datname FROM pg_database WHERE datname IN ('${DB_STAGING}', '${DB_PREVIOUS}') ORDER BY 1")" ||
      die "не удалось прочитать список баз" "make ps; make logs SERVICE=${DB_SERVICE}"
    [[ -z "$found" ]] || prefix_lines "база " <<<"$found"
  fi
  for volume in "${volumes[@]}"; do
    found="$(run_in_volume "$volume" ":ro" sh -c "cd /vol && ls -d ${VOLUME_STAGING} ${VOLUME_READY} 2>/dev/null || true" </dev/null)" ||
      die "не удалось прочитать том $(volume_name "$volume")" "docker volume ls"
    [[ -z "$found" ]] || prefix_lines "том $(volume_name "$volume"): " <<<"$found"
  done
}

# ---------------------------------------------------------------- создание копии

dump_database() {
  local file="$1/${DB_DUMP}"
  log "база портала → ${file}"
  db_run pg_dump -U "$DB_USER" -d "$DB_NAME" --format=custom >"$file" ||
    die "pg_dump не выполнен" "сервис ${DB_SERVICE} должен быть запущен: make ps"
}

archive_volume() {
  local volume="$1" file="$2/$1.tgz"
  log "том $(volume_name "$volume") → ${file}"
  # Каталог прерванного восстановления в копию не попадает.
  # --numeric-owner: владельцы по номерам, независимо от /etc/passwd образа-помощника.
  run_in_volume "$volume" ":ro" tar czf - --numeric-owner -C /vol \
    --exclude "./${VOLUME_STAGING}" --exclude "./${VOLUME_READY}" . </dev/null >"$file" ||
    die "не удалось заархивировать том $(volume_name "$volume")" "проверить место на диске"
}

cmd_create() {
  local target="$1" dest partial traces
  [[ -d "$target" ]] || die "нет каталога ${target}" "создать каталог вне репозитория, например sudo install -d -m 700 /var/backups/llm"
  load_project
  check_prerequisites
  # Иначе копия снялась бы со смешанных данных, а сервисы запустились бы на них.
  traces="$(restore_traces)"
  [[ -z "$traces" ]] ||
    die "восстановление не завершено (${traces//$'\n'/; }): копию с таких данных снимать нельзя" "$RETRY_HINT"
  # Копия содержит личные данные: каталог и файлы доступны только владельцу.
  umask 077
  dest="$(cd "$target" && pwd)/${BACKUP_PREFIX}$(date +%Y%m%d-%H%M%S)"
  partial="${dest}${PARTIAL_SUFFIX}"
  mkdir "$partial"

  # Сервисы возвращаются в работу и при сбое копирования.
  trap start_writers EXIT
  stop_writers
  if has_service "$DB_SERVICE"; then
    dump_database "$partial"
  fi
  local volume
  for volume in "${volumes[@]}"; do
    archive_volume "$volume" "$partial"
  done
  trap - EXIT
  start_writers
  (cd "$partial" && sha256 "${copy_files[@]}" >"$CHECKSUMS")
  mv "$partial" "$dest"

  cat <<EOF

Копия готова: ${dest}
$(cd "$dest" && du -h -- "${copy_files[@]}")

Перенесите каталог на другую машину и храните как секрет: в нём личные данные.
deploy/.env в копию не входит: PORTAL_SECRET_KEY и BIFROST_ENCRYPTION_KEY должны
храниться отдельно, вне ВМ, — без них копия восстановится не полностью.
EOF
}

# ---------------------------------------------------------------- восстановление

# Копия проверяется целиком до подтверждения: данные сервиса ещё не тронуты.
verify_copy() {
  local source="$1" file
  local hint="взять другую копию; эта оборвалась при создании или повреждена при переносе"
  [[ "$source" != *"$PARTIAL_SUFFIX" ]] ||
    die "${source} — незавершённая копия (${PARTIAL_SUFFIX})" "$hint"
  [[ -f "${source}/${CHECKSUMS}" ]] ||
    die "в копии нет ${CHECKSUMS}: её целостность нельзя проверить" \
      "взять копию, созданную make backup; ${CHECKSUMS} пишется последним, без него копия не завершена"
  for file in "${copy_files[@]}"; do
    [[ -s "${source}/${file}" ]] ||
      die "в копии нет ${file} (или файл пуст)" "проверить каталог копии; набор профилей в deploy/.env должен быть тем же, что при её создании"
    grep -q "  ${file}\$" "${source}/${CHECKSUMS}" ||
      die "в ${CHECKSUMS} нет записи о ${file}" "$hint"
  done
  log "проверка контрольных сумм"
  (cd "$source" && sha256 -c "$CHECKSUMS" >/dev/null) ||
    die "контрольные суммы копии не совпадают" "$hint"
  log "проверка читаемости дампа и архивов"
  for file in "${copy_files[@]}"; do
    if [[ "$file" == "$DB_DUMP" ]]; then
      db_exec pg_restore --list <"${source}/${file}" >/dev/null ||
        die "${file} не читается как дамп PostgreSQL" "$hint"
    else
      { gzip -t "${source}/${file}" && tar tzf "${source}/${file}" >/dev/null; } ||
        die "${file} не читается как архив" "$hint"
    fi
  done
}

confirm_restore() {
  local source="$1" answer=""
  echo "Данные сервиса (проект ${project}) будут ЗАМЕНЕНЫ содержимым ${source}: ${copy_files[*]}"
  # Конец ввода без ответа — тоже отказ, а не молчаливый выход по set -e.
  read -r -p "Продолжить? Введите yes: " answer || true
  [[ "$answer" == "yes" ]] || die "восстановление отменено" "для продолжения ввести yes"
}

# Дамп загружается во временную базу: рабочая база при сбое остаётся прежней.
stage_database() {
  log "$1/${DB_DUMP} → временная база ${DB_STAGING}"
  db_run dropdb -U "$DB_USER" --if-exists --force "$DB_STAGING" &&
    db_run createdb -U "$DB_USER" "$DB_STAGING" &&
    db_exec pg_restore -U "$DB_USER" -d "$DB_STAGING" --exit-on-error <"$1/${DB_DUMP}"
}

# Архив распаковывается во временный каталог тома: прежнее содержимое не тронуто.
# Отметка ставится последней: её наличие значит, что распаковка дошла до конца.
stage_volume() {
  log "$2/$1.tgz → временный каталог тома $(volume_name "$1")"
  run_in_volume "$1" "" sh -c "
    set -e
    cd /vol
    rm -rf ${VOLUME_STAGING} ${VOLUME_READY}
    mkdir ${VOLUME_STAGING}
    tar xzf - --numeric-owner -C ${VOLUME_STAGING}
    touch ${VOLUME_READY}
  " <"$2/$1.tgz"
}

# Распакованное на месте (том мог не сохранить его между запусками контейнеров).
volume_staged() {
  run_in_volume "$1" ":ro" sh -c "test -d /vol/${VOLUME_STAGING} && test -f /vol/${VOLUME_READY}" </dev/null
}

discard_staging() {
  local volume
  for volume in "${volumes[@]}"; do
    run_in_volume "$volume" "" rm -rf "/vol/${VOLUME_STAGING}" "/vol/${VOLUME_READY}" </dev/null || true
  done
  if has_service "$DB_SERVICE"; then
    db_run dropdb -U "$DB_USER" --if-exists --force "$DB_STAGING" || true
  fi
}

# Пишущие сервисы остановлены, поэтому открытым может быть только посторонний сеанс
# (psql оператора): он не должен мешать переименованию. Затем одна транзакция: рабочей
# базой становится либо новая, либо остаётся прежняя.
switch_database() {
  log "переключение базы портала"
  db_run dropdb -U "$DB_USER" --if-exists --force "$DB_PREVIOUS" &&
    db_run psql -q -At -o /dev/null -v ON_ERROR_STOP=1 -U "$DB_USER" -d postgres -c \
      "SELECT pg_terminate_backend(pid, 10000) FROM pg_stat_activity WHERE datname IN ('${DB_NAME}', '${DB_STAGING}') AND pid <> pg_backend_pid()" &&
    db_run psql -q -v ON_ERROR_STOP=1 --single-transaction -U "$DB_USER" -d postgres \
      -c "ALTER DATABASE ${DB_NAME} RENAME TO ${DB_PREVIOUS}" \
      -c "ALTER DATABASE ${DB_STAGING} RENAME TO ${DB_NAME}"
}

# Прежнее содержимое удаляется, только если новое лежит в томе целиком: проверка и
# удаление — в одном запуске контейнера. Проверки — отдельными командами: в списке
# через && отказ первой не остановил бы скрипт по set -e.
switch_volume() {
  log "переключение тома $(volume_name "$1")"
  run_in_volume "$1" "" sh -c "
    set -e
    cd /vol
    test -d ${VOLUME_STAGING}
    test -f ${VOLUME_READY}
    find . -mindepth 1 -maxdepth 1 ! -name ${VOLUME_STAGING} ! -name ${VOLUME_READY} -exec rm -rf {} +
    chown \"\$(stat -c %u:%g ${VOLUME_STAGING})\" .
    chmod \"\$(stat -c %a ${VOLUME_STAGING})\" .
    find ${VOLUME_STAGING} -mindepth 1 -maxdepth 1 -exec mv {} . \;
    rmdir ${VOLUME_STAGING}
    rm ${VOLUME_READY}
  " </dev/null
}

# Отказ с сообщением по фактическому состоянию, а не по месту сбоя. Данные прежние,
# только если ни этот запуск, ни прерванный до него не начинали переключение.
abort_restore() {
  local reason="$1"
  if [[ -z "$traces_before" && "$switching" -eq 0 ]]; then
    discard_staging
    if [[ "$writers_stopped" -eq 1 ]]; then
      start_writers
    fi
    die "${reason}; данные сервиса НЕ изменены, сервисы работают на прежних данных" \
      "устранить причину (место на диске, make logs SERVICE=${DB_SERVICE}) и повторить восстановление"
  fi
  local state="Этим запуском переключено на данные копии: ${switched[*]:-ничего}"
  if [[ -n "$traces_before" ]]; then
    state="${state}. До него восстановление уже было прервано (${traces_before//$'\n'/; })"
  fi
  die "${reason}. ${state}. Данные могут быть смешанными: часть из копии, часть прежние. Сервисы ${writers[*]} этим запуском не запущены" \
    "$RETRY_HINT"
}

# Сигнал (Ctrl-C, обрыв сеанса) — тот же отказ по фактическому состоянию. Повторный
# сигнал уборку не прерывает.
interrupt_restore() {
  trap '' INT TERM HUP
  abort_restore "восстановление прервано"
}

cmd_restore() {
  local source="$1" volume
  [[ -d "$source" ]] || die "нет каталога ${source}" "указать каталог копии (${BACKUP_PREFIX}...)"
  source="$(cd "$source" && pwd)"
  load_project
  check_prerequisites
  verify_copy "$source"
  confirm_restore "$source"

  traces_before="$(restore_traces)"
  switching=0
  writers_stopped=0
  switched=()
  if [[ -n "$traces_before" ]]; then
    log "найдены следы прерванного восстановления (${traces_before//$'\n'/; }): этот запуск доведёт его до конца"
  fi

  # Подготовка: новое содержимое кладётся рядом с прежним.
  trap interrupt_restore INT TERM HUP
  if has_service "$DB_SERVICE"; then
    stage_database "$source" || abort_restore "дамп не загрузился во временную базу"
  fi
  # Отметка — до остановки: прерванная остановка тоже требует запуска сервисов.
  writers_stopped=1
  stop_writers
  for volume in "${volumes[@]}"; do
    stage_volume "$volume" "$source" || abort_restore "архив ${volume}.tgz не распаковался в том"
  done
  for volume in "${volumes[@]}"; do
    volume_staged "$volume" ||
      abort_restore "распакованное содержимое не сохранилось в томе ${volume}"
  done

  # Переключение: короткие операции без чтения копии. Сигнал не говорит, успела ли
  # выполниться прерванная операция, поэтому данные с этого места считаются изменёнными.
  trap 'switching=1; interrupt_restore' INT TERM HUP
  if has_service "$DB_SERVICE"; then
    switch_database || abort_restore "база портала не переключена"
    switching=1
    switched+=("база портала")
  fi
  for volume in "${volumes[@]}"; do
    switching=1
    switch_volume "$volume" || abort_restore "том ${volume} переключён не полностью"
    switched+=("том ${volume}")
  done
  if has_service "$DB_SERVICE"; then
    db_run dropdb -U "$DB_USER" --if-exists --force "$DB_PREVIOUS" ||
      abort_restore "прежняя база ${DB_PREVIOUS} не удалена"
  fi
  trap - INT TERM HUP
  start_writers

  cat <<EOF

Восстановление завершено. В deploy/.env должны стоять те же PORTAL_SECRET_KEY и
BIFROST_ENCRYPTION_KEY, что при создании копии. Проверка: make ps; make smoke-portal.
EOF
}

main() {
  case "${1:-}" in
    -h | --help)
      usage
      exit 0
      ;;
    create | restore)
      [[ $# -eq 2 ]] || die "$1 требует каталог" "bash scripts/backup.sh --help"
      command -v docker >/dev/null 2>&1 || die "не найден docker" "запустить на ВМ"
      "cmd_$1" "$2"
      ;;
    *) die "ожидается create или restore" "bash scripts/backup.sh --help" ;;
  esac
}

main "$@"
