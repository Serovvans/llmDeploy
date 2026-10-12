# Песочница проверки SQL (docs/portal-design.md §3.3, §8.1; требования к сервису —
# docs/portal-api.md §7.4). Всё, что в ней исполняется, считается враждебным кодом.

# Тот же тег, что у portal-db в deploy/docker-compose.yml: при обновлении менять оба.
FROM postgres:16.15-bookworm

# Версия пакета из репозитория PGDG, уже подключённого в базовом образе. Обновляется
# вместе с образом: старые сборки пакета из репозитория со временем убирают.
ARG POSTGIS_VERSION=3.6.4+dfsg-2.pgdg12+1

# В образе остаются файлы только двух расширений — plpgsql и postgis. Роли проверки
# имеют право CREATE на базу (схема проверки создаётся в её транзакции), а с ним любая
# роль ставит расширение с пометкой trusted; запретить это правами нельзя, поэтому
# остального из contrib и пакета PostGIS (включая dblink и postgres_fdw) здесь нет.
# postgis.control в пакете — ссылка (update-alternatives) на postgis-3.control: ссылка
# заменяется самим файлом, иначе сервер показывал бы третье расширение «postgis-3».
# Чистка идёт и по ссылкам: от удалённых расширений остались бы висячие.
# Вывод сервера отключён, и причины отказа CREATE EXTENSION в журнале не было бы —
# поэтому управляющий файл проверяется при сборке.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "postgresql-16-postgis-3=${POSTGIS_VERSION}" \
        "postgresql-16-postgis-3-scripts=${POSTGIS_VERSION}" \
    && rm -rf /var/lib/apt/lists/* \
    && cd /usr/share/postgresql/16/extension \
    && cp --remove-destination "$(readlink -f postgis.control)" postgis.control \
    && find . ! -type d \
        ! -name 'plpgsql.control' ! -name 'plpgsql--*' \
        ! -name 'postgis.control' ! -name 'postgis--*' \
        -delete \
    && test -s postgis.control

# Без права на исполнение: такой сценарий entrypoint базового образа подключает (source),
# и в конце он убирает пароли из окружения будущего сервера.
COPY --chmod=0644 init-sandbox.sh /docker-entrypoint-initdb.d/10-sandbox.sh
COPY --chmod=0755 entrypoint.sh /usr/local/bin/sandbox-entrypoint.sh

ENV POSTGRES_DB=sandbox
# Сразу под пользователем postgres: entrypoint базового образа не меняет владельцев и не
# сбрасывает привилегии, поэтому контейнер работает с cap_drop: ALL.
USER postgres

ENTRYPOINT ["sandbox-entrypoint.sh"]
# Параметры заданы при старте сервера: роль проверки изменить их не может.
# - max_worker_processes=0: параллельным исполнителям неоткуда взять процесс, что бы роль
#   ни выставила в max_parallel_workers; max_logical_replication_workers=0 — чтобы сервер
#   не запускал свой фоновый процесс логической репликации.
# - max_prepared_transactions=0: подготовленная транзакция пережила бы сеанс проверки.
# - restart_after_crash=off: аварийное завершение сеанса завершает сервер, и контейнер
#   стартует заново с пустым tmpfs. При восстановлении на месте файлы незафиксированных
#   таблиц убитой транзакции остались бы в tmpfs навсегда.
# - log_*: вторая линия после отключённого вывода (entrypoint.sh) — без текста операторов.
# - shared_buffers, min_wal_size, max_wal_size: каталог данных лежит в tmpfs и считается
#   памятью контейнера; буферы и запас WAL уменьшены под его предел (compose).
CMD ["postgres", \
     "-c", "max_worker_processes=0", \
     "-c", "max_logical_replication_workers=0", \
     "-c", "max_prepared_transactions=0", \
     "-c", "restart_after_crash=off", \
     "-c", "log_statement=none", \
     "-c", "log_min_duration_statement=-1", \
     "-c", "log_min_error_statement=panic", \
     "-c", "log_min_messages=fatal", \
     "-c", "log_connections=off", \
     "-c", "log_disconnections=off", \
     "-c", "shared_buffers=32MB", \
     "-c", "min_wal_size=32MB", \
     "-c", "max_wal_size=64MB"]
