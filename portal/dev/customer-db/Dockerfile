# Тестовая «база заказчика» стенда (docs/portal-api.md §13.5): PostgreSQL с PostGIS,
# на ней проверяются подключения к базам и проверка прав ролей (§7.6).

# Тот же тег и та же версия PostGIS, что у deploy/sql-sandbox: при обновлении менять все.
FROM postgres:16.15-bookworm

ARG POSTGIS_VERSION=3.6.4+dfsg-2.pgdg12+1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        "postgresql-16-postgis-3=${POSTGIS_VERSION}" \
        "postgresql-16-postgis-3-scripts=${POSTGIS_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

COPY --chmod=0644 init/ /docker-entrypoint-initdb.d/

# Журнал операторов с именем приложения и ролью — по нему стендовый тест проверяет, что
# портал шлёт только свои запросы в транзакции READ ONLY (test_customer_db.py).
CMD ["postgres", "-c", "log_statement=all", "-c", "log_line_prefix=%a %u "]
