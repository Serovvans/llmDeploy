#!/usr/bin/env bash
# Стенд портала без GPU (portal/dev, docs/portal-design.md §9). Обёртки — make dev-up,
# dev-reset, dev-down.
#
#   bash scripts/dev_stand.sh up      собрать образы из текущего дерева, поднять, дождаться healthy
#   bash scripts/dev_stand.sh down    остановить и удалить контейнеры; тома остаются
#   bash scripts/dev_stand.sh reset   down, удалить тома с данными (база, векторы, файлы), up
#
# reset сохраняет том caddy-data: в нём корневой CA Caddy стенда, и исключение
# сертификата в браузере не слетает.
#
# Проект compose задаётся явно (-p) именем из строки name: файла стенда. Иначе
# COMPOSE_PROJECT_NAME из окружения или portal/dev/.env был бы сильнее этой строки, и
# команды ушли бы в чужой проект — на машине с рабочим стеком (те же имена сервисов и
# томов) reset удалил бы его данные. На другой файл compose скрипт не перенаправляется.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly COMPOSE_FILE_PATH="${SCRIPT_DIR}/../portal/dev/docker-compose.yml"
readonly KEPT_VOLUME="caddy-data"

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
  docker compose -p "$project" -f "$COMPOSE_FILE_PATH" "$@"
}

stand_up() {
  compose up -d --build --wait
}

# Удаляет тома данных; стенд к этому моменту остановлен.
remove_data_volumes() {
  local key name
  for key in $1; do
    # По меткам compose: настоящее имя тома, и только если том существует.
    for name in $(docker volume ls -q \
      --filter "label=com.docker.compose.project=${project}" \
      --filter "label=com.docker.compose.volume=${key}"); do
      log "удаление тома ${name}"
      docker volume rm "$name" >/dev/null
    done
  done
  log "данные стенда удалены; том ${project}_${KEPT_VOLUME} сохранён"
}

main() {
  case "${1:-}" in
    -h | --help)
      usage
      exit 0
      ;;
    up | down | reset) ;;
    *) die "ожидается up, down или reset" "bash scripts/dev_stand.sh --help" ;;
  esac
  command -v docker >/dev/null 2>&1 || die "не найден docker" "установить и запустить Docker"
  project="$(sed -n 's/^name: *//p' "$COMPOSE_FILE_PATH" | head -n 1 | tr -d "\"'")"
  [[ -n "$project" ]] ||
    die "в ${COMPOSE_FILE_PATH} нет строки name: — проект стенда не определить" "вернуть строку name: в файл compose стенда"

  case "$1" in
    up) stand_up ;;
    down) compose down ;;
    reset)
      # Список томов читается до остановки: при ошибке конфигурации стенд остаётся как был.
      local volume_keys
      volume_keys="$(compose config --volumes)" ||
        die "docker compose не читает ${COMPOSE_FILE_PATH}" "проверить portal/dev/.env (образец — portal/dev/.env.example)"
      log "остановка стенда (проект ${project})"
      compose down
      remove_data_volumes "$(grep -vx "$KEPT_VOLUME" <<<"$volume_keys" || true)"
      stand_up
      ;;
  esac
}

main "$@"
