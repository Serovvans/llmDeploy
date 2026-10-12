# shellcheck shell=bash
# Инициализация песочницы SQL: расширение, роли, права, pg_hba.conf
# (docs/portal-api.md §7.4, таблица требований к сервису sql-sandbox).
#
# Сценарий подключается (source) entrypoint'ом базового образа после initdb, пока
# временный сервер слушает только локальный сокет. Каталог данных лежит в tmpfs, поэтому
# сценарий выполняется при каждом старте контейнера. Ошибка любой команды прерывает
# запуск: entrypoint работает с set -e.

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

# Пароли psql берёт из окружения (\getenv), а не из аргументов командной строки.
docker_process_sql -v roles="$SQL_SANDBOX_ROLES" <<'SQL'
\getenv run_password SQL_SANDBOX_PASSWORD
\getenv control_password SQL_SANDBOX_CONTROL_PASSWORD

-- Ставит суперпользователь: роли проверки расширений не создают.
CREATE EXTENSION postgis;

-- У PUBLIC по умолчанию есть CONNECT и TEMPORARY на базу и USAGE на схему public;
-- права выдаются ролям поимённо.
REVOKE ALL ON DATABASE :"DBNAME" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

-- Одним разрешённым SELECT функция пишет в WAL сколько угодно и заполняет tmpfs.
SELECT format('REVOKE EXECUTE ON FUNCTION %s FROM PUBLIC', oid::regprocedure)
FROM pg_proc
WHERE proname = 'pg_logical_emit_message' AND pronamespace = 'pg_catalog'::regnamespace
\gexec

-- Роли проверки: по одной на место, чтобы одновременные проверки не видели текст
-- запроса друг друга в pg_stat_activity и не могли снять чужой сеанс. Два соединения —
-- идущая попытка и ещё не закрывшийся сеанс предыдущей. CREATE на базу — только ради
-- схемы проверки, которая создаётся в её транзакции.
SELECT
    format(
        'CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
        ' CONNECTION LIMIT 2 PASSWORD %L',
        role_name, :'run_password'
    ),
    format('GRANT CONNECT, CREATE ON DATABASE %I TO %I', current_database(), role_name),
    format('GRANT USAGE ON SCHEMA public TO %I', role_name)
FROM (SELECT 'sandbox_run_' || n AS role_name FROM generate_series(1, :roles) AS n) AS run_roles
\gexec

-- Значения на случай, если портал не выставил свои; всё, кроме temp_file_limit, роль
-- меняет сама, поэтому жёсткая граница — пределы контейнера.
SELECT format('ALTER ROLE %I SET %I = %L', 'sandbox_run_' || n, name, value)
FROM generate_series(1, :roles) AS n,
    (VALUES
        ('statement_timeout', '5s'),
        ('lock_timeout', '2s'),
        ('idle_in_transaction_session_timeout', '30s'),
        ('work_mem', '8MB'),
        ('temp_file_limit', '32MB'),
        ('max_parallel_workers_per_gather', '0')
    ) AS defaults (name, value)
\gexec

-- Управляющая роль: под ней портал только завершает сеансы ролей проверки. В pg_monitor
-- и pg_read_all_stats не входит — текст чужих запросов ей не виден.
SELECT format(
    'CREATE ROLE sandbox_ctl LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION'
    ' CONNECTION LIMIT %s PASSWORD %L',
    :roles, :'control_password'
)
\gexec
GRANT CONNECT ON DATABASE :"DBNAME" TO sandbox_ctl;
GRANT pg_signal_backend TO sandbox_ctl;
SQL

# Файл целиком свой: entrypoint дописывает в исходный строку «host all all all», а initdb
# оставляет доступ trust с адресов самого контейнера. Суперпользователь входит только
# через локальный сокет; по сети — только роли песочницы и только в её базу.
{
  echo "local all ${POSTGRES_USER} peer"
  echo "host ${POSTGRES_DB} sandbox_ctl all scram-sha-256"
  for ((n = 1; n <= SQL_SANDBOX_ROLES; n++)); do
    echo "host ${POSTGRES_DB} sandbox_run_${n} all scram-sha-256"
  done
} >"${PGDATA}/pg_hba.conf"

# Сервер стартует из этой же оболочки и унаследовал бы пароли в своём окружении.
unset SQL_SANDBOX_PASSWORD SQL_SANDBOX_CONTROL_PASSWORD POSTGRES_PASSWORD n
