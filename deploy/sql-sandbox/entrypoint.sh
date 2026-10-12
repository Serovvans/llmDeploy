#!/bin/bash
# Запуск песочницы SQL поверх entrypoint базового образа postgres.
set -euo pipefail

# Настройки проверяются здесь, а не в init-sandbox.sh: ниже весь вывод базового entrypoint
# уходит в /dev/null, и причины отказа в журнале контейнера не было бы. В этих сообщениях
# нет ни текста операторов, ни значений паролей.
if ! [[ "${SQL_SANDBOX_ROLES:-}" =~ ^[1-9][0-9]*$ ]]; then
  echo "SQL_SANDBOX_ROLES: нужно целое число не меньше 1" >&2
  exit 1
fi
if [[ -z "${SQL_SANDBOX_PASSWORD:-}" || -z "${SQL_SANDBOX_CONTROL_PASSWORD:-}" ]]; then
  echo "SQL_SANDBOX_PASSWORD и SQL_SANDBOX_CONTROL_PASSWORD обязательны" >&2
  exit 1
fi
# Управляющая роль завершает сеансы ролей проверки; с общим паролем утечка одного
# открывала бы обе.
if [[ "$SQL_SANDBOX_PASSWORD" == "$SQL_SANDBOX_CONTROL_PASSWORD" ]]; then
  echo "SQL_SANDBOX_PASSWORD и SQL_SANDBOX_CONTROL_PASSWORD не должны совпадать" >&2
  exit 1
fi

# Суперпользователь нужен только инициализации и входит через локальный сокет
# (init-sandbox.sh). Базовый entrypoint без пароля суперпользователя согласен стартовать
# лишь с доступом trust для всех адресов, поэтому пароль случайный и нигде не хранится.
POSTGRES_PASSWORD="$(head -c 32 /dev/urandom | base64)"
export POSTGRES_PASSWORD

# Вывод сервера не сохраняется вовсе (docs/portal-api.md §7.4, «Журнал сервера»): при
# PANIC PostgreSQL пишет текст оператора в журнал при любых параметрах, а вызвать PANIC
# может сам проверяемый запрос, заполнив tmpfs. О состоянии песочницы говорят проверка
# здоровья и число перезапусков контейнера.
exec docker-entrypoint.sh "$@" >/dev/null 2>&1
