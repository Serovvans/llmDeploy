"""Порт `CustomerDatabase` на asyncpg: обращения к базам заказчика (docs/portal-api.md §7.6.5).

Каждое обращение — место по `host:port`, одно соединение и одна транзакция
`REPEATABLE READ READ ONLY`, первыми в ней — запросы проверки прав §7.6.6, в конце —
`ROLLBACK` и закрытие, в том числе при ошибке и отмене. В базу уходят только операторы
этого модуля: значения — параметрами, имена — из снимка идентификаторами в кавычках,
функции и типы — с именем схемы. Пароль, адрес и имена объектов в журнал не пишутся.
"""

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import asyncpg

from portal.core.settings import DbSettings
from portal.databases.domain import FINDING_KINDS, RightsFinding, finding
from portal.databases.hosts import HostPolicy
from portal.databases.ports import (
    DbReason,
    DbRightsExcessError,
    DbTarget,
    DbUnavailableError,
    RightsReport,
    SampleRequest,
    SampleRows,
    SnapshotContent,
)

_ROLES = """
WITH r AS (
  SELECT oid, rolname FROM pg_catalog.pg_roles
  WHERE pg_catalog.pg_has_role(current_user, oid, 'MEMBER')
)
"""
_ATTRIBUTES = (
    _ROLES
    + """
SELECT r.rolname, p.rolsuper, p.rolcreaterole, p.rolcreatedb, p.rolreplication, p.rolbypassrls
FROM r JOIN pg_catalog.pg_roles p ON p.oid = r.oid
WHERE p.rolsuper OR p.rolcreaterole OR p.rolcreatedb OR p.rolreplication OR p.rolbypassrls
"""
)
_OWNERSHIP = (
    _ROLES
    + """
SELECT o.type, o.identity, count(*) OVER () AS total
FROM pg_catalog.pg_shdepend d
CROSS JOIN LATERAL pg_catalog.pg_identify_object(d.classid, d.objid, d.objsubid) AS o
WHERE d.deptype = 'o'
  AND d.refclassid = 'pg_catalog.pg_authid'::pg_catalog.regclass
  AND d.refobjid IN (SELECT oid FROM r)
  AND d.dbid IN (0, (SELECT oid FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()))
ORDER BY o.identity
LIMIT 3
"""  # noqa: E501
)
# Отступление от §7.6.6, запрос 3: `UPDATE` на `pg_catalog.pg_settings` PostgreSQL сам
# выдаёт `PUBLIC` (это `set_config`, данных не пишет) — без исключения любая роль «лишняя».
_WRITE = (
    _ROLES
    + """
SELECT c.relkind, pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(c.relname) AS name,
       ARRAY(
         SELECT p FROM pg_catalog.unnest(ARRAY['INSERT','UPDATE','DELETE','TRUNCATE','REFERENCES','TRIGGER','USAGE']) WITH ORDINALITY AS u(p, i)
         WHERE EXISTS (SELECT 1 FROM r WHERE CASE
           WHEN c.relkind = 'S' THEN CASE WHEN p IN ('UPDATE','USAGE') THEN pg_catalog.has_sequence_privilege(r.oid, c.oid, p) ELSE false END
           WHEN p = 'USAGE' THEN false
           WHEN p IN ('INSERT','UPDATE','REFERENCES') THEN pg_catalog.has_any_column_privilege(r.oid, c.oid, p)
           ELSE pg_catalog.has_table_privilege(r.oid, c.oid, p) END)
         ORDER BY i) AS privileges,
       count(*) OVER () AS total
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  AND NOT (n.nspname = 'pg_catalog' AND c.relname = 'pg_settings')
  AND EXISTS (SELECT 1 FROM r WHERE CASE
    WHEN c.relkind = 'S' THEN pg_catalog.has_sequence_privilege(r.oid, c.oid, 'UPDATE')
      OR pg_catalog.has_sequence_privilege(r.oid, c.oid, 'USAGE')
    ELSE pg_catalog.has_any_column_privilege(r.oid, c.oid, 'INSERT')
      OR pg_catalog.has_any_column_privilege(r.oid, c.oid, 'UPDATE')
      OR pg_catalog.has_any_column_privilege(r.oid, c.oid, 'REFERENCES')
      OR pg_catalog.has_table_privilege(r.oid, c.oid, 'DELETE')
      OR pg_catalog.has_table_privilege(r.oid, c.oid, 'TRUNCATE')
      OR pg_catalog.has_table_privilege(r.oid, c.oid, 'TRIGGER') END)
ORDER BY name
LIMIT 3
"""  # noqa: E501
)
_CREATE = (
    _ROLES
    + """
SELECT 'database' AS type, pg_catalog.current_database()::pg_catalog.text AS name
WHERE EXISTS (SELECT 1 FROM r WHERE pg_catalog.has_database_privilege(r.oid, pg_catalog.current_database(), 'CREATE'))
UNION ALL
SELECT 'schema', n.nspname::pg_catalog.text FROM pg_catalog.pg_namespace n
WHERE EXISTS (SELECT 1 FROM r WHERE pg_catalog.has_schema_privilege(r.oid, n.oid, 'CREATE'))
"""  # noqa: E501
)
_PREDEFINED = (
    _ROLES
    + """
SELECT rolname FROM r
WHERE rolname LIKE 'pg\\_%' AND rolname NOT IN ('pg_read_all_data', 'pg_read_all_settings',
  'pg_read_all_stats', 'pg_stat_scan_tables', 'pg_monitor', 'pg_database_owner')
ORDER BY rolname
"""
)
_DEFINER = (
    _ROLES
    + """
SELECT pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(p.proname) AS name, count(*) OVER () AS total
FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid = p.pronamespace
WHERE p.prosecdef AND p.prokind IN ('f', 'p')
  AND p.prorettype NOT IN ('pg_catalog.trigger'::pg_catalog.regtype, 'pg_catalog.event_trigger'::pg_catalog.regtype)
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND p.proowner NOT IN (SELECT oid FROM r)
  AND EXISTS (SELECT 1 FROM r WHERE pg_catalog.has_function_privilege(r.oid, p.oid, 'EXECUTE'))
ORDER BY name
LIMIT 3
"""  # noqa: E501
)
_VERSION = "SELECT pg_catalog.current_setting('server_version_num')::pg_catalog.int4"
# С этой версии есть `pg_parameter_acl` — права на параметры сервера (§7.6.6, запросы 8, 10).
_PARAMETER_ACL_VERSION = 150000
# Запрос 7: по спискам прав, а не `has_table_privilege` — на PostgreSQL ≤ 16 права
# `MAINTAIN` нет, функция с ним дала бы ошибку, а сравнение строки — нет.
_MAINTAIN = (
    _ROLES
    + """
SELECT c.relkind, pg_catalog.quote_ident(n.nspname) || '.' || pg_catalog.quote_ident(c.relname) AS name,
       count(*) OVER () AS total
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND EXISTS (SELECT 1 FROM pg_catalog.aclexplode(c.relacl) AS a
              WHERE a.privilege_type = 'MAINTAIN'
                AND (a.grantee = 0::pg_catalog.oid OR a.grantee IN (SELECT oid FROM r)))
ORDER BY name
LIMIT 3
"""  # noqa: E501
)
# Запрос 8: ветви по каталогам со списками прав — (класс, таблица, oid, столбец прав).
_GRANTABLE_CATALOGS = (
    ("pg_namespace", "pg_namespace", "oid", "nspacl"),
    ("pg_proc", "pg_proc", "oid", "proacl"),
    ("pg_type", "pg_type", "oid", "typacl"),
    ("pg_language", "pg_language", "oid", "lanacl"),
    ("pg_foreign_data_wrapper", "pg_foreign_data_wrapper", "oid", "fdwacl"),
    ("pg_foreign_server", "pg_foreign_server", "oid", "srvacl"),
    ("pg_largeobject", "pg_largeobject_metadata", "oid", "lomacl"),
    ("pg_database", "pg_database", "oid", "datacl"),
    ("pg_tablespace", "pg_tablespace", "oid", "spcacl"),
)
_PARAMETER_CATALOG = ("pg_parameter_acl", "pg_parameter_acl", "oid", "paracl")


def _grant_option_query(parameters: bool) -> str:
    """Запрос 8 одного из двух постоянных видов: с ветвью `pg_parameter_acl` или без."""
    branches = [
        "SELECT 'pg_catalog.pg_class'::pg_catalog.regclass AS classid, c.oid AS objid,"
        " 0 AS objsubid, a.privilege_type\n"
        "FROM pg_catalog.pg_class c CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) AS a\n"
        "WHERE a.is_grantable AND a.grantee IN (SELECT oid FROM r)",
        "SELECT 'pg_catalog.pg_class'::pg_catalog.regclass, t.attrelid, t.attnum,"
        " a.privilege_type\n"
        "FROM pg_catalog.pg_attribute t CROSS JOIN LATERAL pg_catalog.aclexplode(t.attacl) AS a\n"
        "WHERE t.attacl IS NOT NULL AND a.is_grantable AND a.grantee IN (SELECT oid FROM r)",
    ]
    catalogs = _GRANTABLE_CATALOGS + ((_PARAMETER_CATALOG,) if parameters else ())
    for klass, table, key, acl in catalogs:
        branches.append(
            f"SELECT 'pg_catalog.{klass}'::pg_catalog.regclass, x.{key}, 0, a.privilege_type\n"
            f"FROM pg_catalog.{table} x CROSS JOIN LATERAL pg_catalog.aclexplode(x.{acl}) AS a\n"
            "WHERE a.is_grantable AND a.grantee IN (SELECT oid FROM r)"
        )
    union = "\n  UNION ALL\n".join(branches)
    return (
        _ROLES
        + f""", g AS (
{union}
)
SELECT o.type, o.identity AS name,
       pg_catalog.array_agg(DISTINCT g.privilege_type ORDER BY g.privilege_type) AS privileges,
       count(*) OVER () AS total
FROM g CROSS JOIN LATERAL pg_catalog.pg_identify_object(g.classid, g.objid, g.objsubid) AS o
GROUP BY o.type, o.identity
ORDER BY o.identity
LIMIT 3
"""
    )


_GRANT_OPTION = {True: _grant_option_query(True), False: _grant_option_query(False)}
# Запрос 9: прямо по `pg_auth_members` — `admin_option` есть во всех версиях.
_ADMIN_OPTION = (
    _ROLES
    + """
SELECT DISTINCT g.rolname
FROM pg_catalog.pg_auth_members m JOIN pg_catalog.pg_roles g ON g.oid = m.roleid
WHERE m.admin_option AND m.member IN (SELECT oid FROM r)
ORDER BY g.rolname
"""
)
# Запрос 10: только на PostgreSQL 15+ — раньше каталога `pg_parameter_acl` нет.
_SERVER_SETTINGS = (
    _ROLES
    + """
SELECT p.parname AS name,
       pg_catalog.array_agg(DISTINCT a.privilege_type ORDER BY a.privilege_type) AS privileges,
       count(*) OVER () AS total
FROM pg_catalog.pg_parameter_acl p CROSS JOIN LATERAL pg_catalog.aclexplode(p.paracl) AS a
WHERE a.privilege_type IN ('ALTER SYSTEM', 'SET')
  AND (a.grantee = 0::pg_catalog.oid OR a.grantee IN (SELECT oid FROM r))
GROUP BY p.parname
ORDER BY p.parname
LIMIT 3
"""
)
# Запрос 11 (А-153, решение 44): `EXECUTE` на функцию любой схемы, закрытую для `PUBLIC`
# начальными правами (`pg_init_privs` от `initdb` и `CREATE EXTENSION`, нет строки —
# `acldefault`), выданный роли из `R` или `PUBLIC` сверх начального списка. Штатного списка
# функций нет — он разошёлся бы с версиями. `search_path` обращения — `pg_catalog`, поэтому
# `regprocedure` пишет имя вне `pg_catalog` со схемой (`public.dblink_connect_u(text)`).
_CATALOG_FUNCTION = (
    _ROLES
    + """
SELECT p.oid::pg_catalog.regprocedure::pg_catalog.text AS name, count(*) OVER () AS total
FROM pg_catalog.pg_proc p
LEFT JOIN pg_catalog.pg_init_privs i
  ON i.objoid = p.oid AND i.classoid = 'pg_catalog.pg_proc'::pg_catalog.regclass AND i.objsubid = 0
CROSS JOIN LATERAL (SELECT COALESCE(i.initprivs, pg_catalog.acldefault('f', p.proowner)) AS init,
                           COALESCE(p.proacl, pg_catalog.acldefault('f', p.proowner)) AS cur) AS d
WHERE NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(d.init) AS b
                  WHERE b.privilege_type = 'EXECUTE' AND b.grantee = 0::pg_catalog.oid)
  AND EXISTS (SELECT 1 FROM pg_catalog.aclexplode(d.cur) AS a
              WHERE a.privilege_type = 'EXECUTE'
                AND (a.grantee = 0::pg_catalog.oid OR a.grantee IN (SELECT oid FROM r))
                AND NOT EXISTS (SELECT 1 FROM pg_catalog.aclexplode(d.init) AS b
                                WHERE b.privilege_type = 'EXECUTE' AND b.grantee = a.grantee))
ORDER BY name
LIMIT 3
"""
)
# Запрос 12: по умолчанию `UPDATE` на большой объект только у владельца (его ловит запрос 2).
_LARGE_OBJECT = (
    _ROLES
    + """
SELECT l.oid::pg_catalog.text AS name, count(*) OVER () AS total
FROM pg_catalog.pg_largeobject_metadata l
WHERE EXISTS (SELECT 1 FROM pg_catalog.aclexplode(l.lomacl) AS a
              WHERE a.privilege_type = 'UPDATE'
                AND (a.grantee = 0::pg_catalog.oid OR a.grantee IN (SELECT oid FROM r)))
ORDER BY l.oid
LIMIT 3
"""
)
# Запрос 13: значение в сеансе обращения, с учётом `ALTER ROLE … SET` и `ALTER DATABASE … SET`.
_LO_COMPAT = "SELECT pg_catalog.current_setting('lo_compat_privileges')::pg_catalog.bool"
_LIMITS = (
    "SELECT pg_catalog.set_config('statement_timeout', $1, true),"
    " pg_catalog.set_config('lock_timeout', $2, true),"
    " pg_catalog.set_config('idle_in_transaction_session_timeout', $3, true)"
)
_MISSING_SCHEMAS = """
SELECT u.s FROM pg_catalog.unnest($1::pg_catalog.text[]) WITH ORDINALITY AS u(s, i)
WHERE NOT EXISTS (
  SELECT 1 FROM pg_catalog.pg_namespace n
  WHERE n.nspname = u.s AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
)
ORDER BY u.i
"""
_TABLES = """
SELECT c.oid, n.nspname, c.relname, c.relkind,
       pg_catalog.left(pg_catalog.obj_description(c.oid, 'pg_class'), $2) AS comment
FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY($1::pg_catalog.text[])
  AND c.relkind IN ('r', 'p', 'v', 'm', 'f') AND NOT c.relispartition
  AND pg_catalog.has_schema_privilege(n.oid, 'USAGE')
  AND pg_catalog.has_any_column_privilege(c.oid, 'SELECT')
ORDER BY pg_catalog.array_position($1::pg_catalog.text[], n.nspname::pg_catalog.text), c.relname
LIMIT $3
"""
_COLUMNS = """
SELECT a.attrelid, a.attnum, a.attname, a.attnotnull,
       pg_catalog.format_type(a.atttypid, a.atttypmod) AS type,
       pg_catalog.left(pg_catalog.col_description(a.attrelid, a.attnum), $2) AS comment,
       t.typtype, tn.nspname AS type_schema, t.typname,
       bt.oid AS base_oid, pg_catalog.format_type(t.typbasetype, t.typtypmod) AS base_type,
       et.oid AS elem_oid, en.nspname AS elem_schema
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace
LEFT JOIN pg_catalog.pg_type bt ON bt.oid = t.typbasetype
LEFT JOIN pg_catalog.pg_type et ON et.oid = t.typelem AND t.typcategory = 'A'
LEFT JOIN pg_catalog.pg_namespace en ON en.oid = et.typnamespace
WHERE a.attrelid = ANY($1::pg_catalog.oid[]) AND a.attnum > 0 AND NOT a.attisdropped
  AND pg_catalog.has_column_privilege(a.attrelid, a.attnum, 'SELECT')
ORDER BY a.attrelid, a.attnum
"""
_CONSTRAINTS = """
SELECT con.conrelid, con.contype, con.conkey, con.confkey, fn.nspname AS ref_schema, fc.relname AS ref_table,
       fc.oid AS ref_oid
FROM pg_catalog.pg_constraint con
LEFT JOIN pg_catalog.pg_class fc ON fc.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace fn ON fn.oid = fc.relnamespace
WHERE con.conrelid = ANY($1::pg_catalog.oid[]) AND con.contype IN ('p', 'f')
"""  # noqa: E501
_POSTGIS = """
SELECT n.nspname FROM pg_catalog.pg_extension e JOIN pg_catalog.pg_namespace n ON n.oid = e.extnamespace
WHERE e.extname = 'postgis'
"""  # noqa: E501

_ATTRIBUTE_KINDS = ("superuser", "createrole", "createdb", "replication", "bypassrls")
_ATTRIBUTE_COLUMNS = ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls")
_RELATION_TYPES = {
    "r": "table",
    "p": "table",
    "v": "view",
    "m": "materialized_view",
    "f": "foreign_table",
    "S": "sequence",
}


def _char(value: bytes | str) -> str:
    """Столбец каталога типа `"char"` (`relkind`, `contype`, `typtype`): asyncpg отдаёт байты."""
    return value.decode() if isinstance(value, bytes) else value


_WRITE_ORDER = ("INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER", "USAGE")
_SPATIAL = re.compile(r"^(?:.+\.)?(geometry|geography)(?:\((\w+)(?:,(\d+))?\))?$")
_SANDBOX_TYPE = re.compile(r"^[a-z][a-z0-9 _]*(\(\d+(,\d+)?\))?[a-z ]*(\[\])*$")
_TEXT = "text"


def root_certificate_readable(path: Path) -> bool:
    """Корневой сертификат заказчика есть и читается (`db.tls_verify_available`)."""
    try:
        path.read_bytes()
    except OSError:
        return False
    return True


def ident(name: str) -> str:
    """Имя идентификатором в кавычках: `"` удваивается (§7.6.5)."""
    return '"' + name.replace('"', '""') + '"'


def _seconds(value: float) -> str:
    return f"{int(value)}s"


def _reason(error: BaseException) -> DbReason:
    """Причина §7.6.4 по исключению драйвера или сети."""
    if isinstance(error, asyncpg.InvalidPasswordError):
        return "auth_failed"
    if isinstance(error, asyncpg.InvalidAuthorizationSpecificationError):
        return "tls_required" if "no encryption" in (error.message or "") else "auth_failed"
    if isinstance(error, asyncpg.InvalidCatalogNameError):
        return "database_not_found"
    if isinstance(error, asyncpg.TooManyConnectionsError):
        return "busy"
    if isinstance(error, (asyncpg.QueryCanceledError, asyncpg.LockNotAvailableError)):
        return "timeout"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, ssl.SSLError):
        return "tls_failed"
    if isinstance(error, OSError):
        return "tls_failed" if "SSL" in str(error) else "unreachable"
    if isinstance(error, asyncpg.ConnectionDoesNotExistError):
        return "unreachable"
    return "other"


def spatial_type(type_text: str) -> tuple[str, str, int] | None:
    """`(geometry|geography, вид, srid)` по `format_type`; не пространственный — `None`."""
    match = _SPATIAL.match(type_text)
    if match is None:
        return None
    return match.group(1), match.group(2) or "Geometry", int(match.group(3) or 0)


def sandbox_type(type_text: str) -> str:
    """Тип столбца в песочнице после сверки (§7.6.7): не прошёл — `text`."""
    spatial = spatial_type(type_text)
    if spatial is not None and "." not in type_text:
        kind, subtype, srid = spatial
        return f"{kind}({subtype},{srid})" if subtype != "Geometry" or srid else kind
    return type_text if _SANDBOX_TYPE.match(type_text) else _TEXT


def _catalog_type(text: str, name: str | None) -> str:
    """Базовый тип `pg_catalog`, кроме `reg*`, — как его пишет `format_type`; иначе `text`."""
    if "." in text or (name or "").startswith("reg"):
        return _TEXT
    return text


def column_types(row: Any, postgis: str | None) -> tuple[str, dict[str, Any] | None, str, bool]:
    """Тип для снимка, геометрия, тип в песочнице и признак двоичного (§7.6.7)."""
    type_text: str = row["type"]
    spatial = None
    if postgis is not None and row["type_schema"] == postgis:
        spatial = spatial_type(type_text)
    if spatial is not None:
        _, subtype, srid = spatial
        shown = type_text.split(".", 1)[1] if "." in type_text.split("(")[0] else type_text
        return shown, {"type": subtype, "srid": srid}, sandbox_type(shown), False
    if row["typname"] == "bytea" and row["type_schema"] == "pg_catalog":
        return type_text, None, "bytea", True
    typtype = _char(row["typtype"])
    if typtype == "d":
        base = row["base_type"] or _TEXT
        base_spatial = spatial_type(base)
        if base_spatial is not None and postgis is not None and base.startswith(f"{postgis}."):
            internal = sandbox_type(base.split(".", 1)[1])
        else:
            internal = _catalog_type(base, base.split("(")[0])
        return type_text, None, sandbox_type(internal), base == "bytea"
    if row["elem_oid"] is not None:
        internal = type_text if row["elem_schema"] == "pg_catalog" else _TEXT
        return type_text, None, sandbox_type(_catalog_type(internal, row["typname"][1:])), False
    if typtype == "b" and row["type_schema"] == "pg_catalog":
        return type_text, None, sandbox_type(_catalog_type(type_text, row["typname"])), False
    return type_text, None, _TEXT, False


Resolver = Callable[[str, int], Awaitable[Sequence[str]]]


async def resolve_host(host: str, port: int) -> list[str]:
    """Адреса имени для TCP — один `getaddrinfo`; адрес как есть — он сам (§7.6.5, шаг 2)."""
    found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(sockaddr[0]) for *_, sockaddr in found]


class NamedTlsContext(ssl.SSLContext):
    """TLS-контекст, который проверяет сертификат по введённому имени, а не по адресу.

    asyncpg берёт имя для TLS из `host`, а соединение идёт на проверенный адрес (DNS
    rebinding): имя подставляется здесь, при создании TLS-объекта соединения.
    """

    server_name: str = ""

    def wrap_bio(
        self,
        incoming: ssl.MemoryBIO,
        outgoing: ssl.MemoryBIO,
        server_side: bool = False,
        server_hostname: str | bytes | None = None,
        session: ssl.SSLSession | None = None,
    ) -> ssl.SSLObject:
        """TLS-объект с именем `server_name` вместо адреса."""
        return super().wrap_bio(
            incoming, outgoing, server_side, self.server_name or server_hostname, session
        )


class AsyncpgCustomerDatabase:
    """Обращения к базам заказчика; места по `host:port` — в памяти процесса."""

    def __init__(
        self, settings: DbSettings, policy: HostPolicy, resolve: Resolver = resolve_host
    ) -> None:
        """Запомнить параметры; соединений до первого обращения нет.

        `policy` — запрет внутренних адресов портала, `resolve` — разрешение имени
        (§7.6.5, шаг 2); тесты подменяют их, а не обходят проверку.
        """
        self._settings = settings
        self._policy = policy
        self._resolve = resolve
        self._places: dict[tuple[str, int], asyncio.Semaphore] = {}

    def _ssl(self, target: DbTarget) -> ssl.SSLContext | bool:
        """TLS по режиму; имя для проверки сертификата — введённое, а не адрес."""
        if target.tls_mode == "disable":
            return False
        context = NamedTlsContext(ssl.PROTOCOL_TLS_CLIENT)
        context.server_name = target.host
        if target.tls_mode == "verify":
            context.load_verify_locations(cafile=str(self._settings.tls_root_cert_file))
            return context
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    async def _addresses(self, target: DbTarget) -> list[str]:
        """Имя разрешается один раз; хоть один адрес запрещён — `host_forbidden` (шаг 2)."""
        if self._policy.name_forbidden(target.host):
            raise DbUnavailableError("host_forbidden")
        addresses = list(dict.fromkeys(await self._resolve(target.host, target.port)))
        if not addresses:
            raise DbUnavailableError("unreachable")
        for address in addresses:
            try:
                parsed = ipaddress.ip_address(address)
            except ValueError:
                raise DbUnavailableError("host_forbidden") from None
            if self._policy.address_forbidden(parsed, target.host):
                raise DbUnavailableError("host_forbidden")
        return addresses

    async def _connect(self, target: DbTarget, tls: ssl.SSLContext | bool) -> Any:
        """Соединение только на проверенные адреса, по порядку до первого удачного.

        Разрешение имени и попытки соединения — в пределах `db.connect_timeout_seconds`.
        """
        settings = self._settings
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.connect_timeout_seconds
        async with asyncio.timeout_at(deadline):
            addresses = await self._addresses(target)
        failure: OSError = OSError("no address accepted the connection")
        for address in addresses:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                return await asyncpg.connect(
                    host=address,
                    port=target.port,
                    user=target.username,
                    password=target.password,
                    database=target.database,
                    timeout=remaining,
                    command_timeout=settings.client_timeout_seconds,
                    ssl=tls,
                    statement_cache_size=0,
                    server_settings={
                        "application_name": settings.application_name,
                        "search_path": "pg_catalog",
                        "statement_timeout": _seconds(settings.statement_timeout_seconds),
                        "lock_timeout": _seconds(settings.lock_timeout_seconds),
                        "idle_in_transaction_session_timeout": _seconds(
                            settings.idle_in_transaction_timeout_seconds
                        ),
                        "default_transaction_read_only": "on",
                    },
                )
            except ssl.SSLError:
                raise
            except OSError as error:
                # Адрес не принял соединение — следующий проверенный адрес того же имени.
                failure = error
        raise failure

    @asynccontextmanager
    async def _access(self, target: DbTarget) -> AsyncIterator[Any]:
        """Место, адрес и соединение, транзакция только для чтения; в конце — откат и закрытие.

        Шаги 2–5 — в пределах `db.access_timeout_seconds`, каждая операция — в пределах
        `db.client_timeout_seconds` (`command_timeout`). Срок истёк — соединение рвётся без
        `ROLLBACK`, причина `timeout`; место освобождается в том же `finally` (§7.6.5).
        """
        settings = self._settings
        place = self._places.setdefault(
            (target.host, target.port), asyncio.Semaphore(settings.max_connections_per_server)
        )
        try:
            async with asyncio.timeout(settings.wait_seconds):
                await place.acquire()
        except TimeoutError:
            raise DbUnavailableError("busy") from None
        connection: Any = None
        expired = False
        try:
            try:
                tls = self._ssl(target)
            except (OSError, ssl.SSLError):
                raise DbUnavailableError("tls_failed") from None
            try:
                async with asyncio.timeout(settings.access_timeout_seconds):
                    connection = await self._connect(target, tls)
                    await connection.transaction(isolation="repeatable_read", readonly=True).start()
                    await connection.fetchrow(
                        _LIMITS,
                        _seconds(settings.statement_timeout_seconds),
                        _seconds(settings.lock_timeout_seconds),
                        _seconds(settings.idle_in_transaction_timeout_seconds),
                    )
                    yield connection
            except (DbRightsExcessError, DbUnavailableError):
                raise
            except TimeoutError:
                expired = True
                raise DbUnavailableError("timeout") from None
            except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as error:
                raise DbUnavailableError(_reason(error)) from None
        finally:
            try:
                if connection is not None:
                    if expired:
                        connection.terminate()
                    else:
                        await self._close(connection)
            finally:
                place.release()

    async def _close(self, connection: Any) -> None:
        """`ROLLBACK` и закрытие; не вышло (обрыв, срок, отмена) — соединение рвётся."""
        try:
            async with asyncio.timeout(self._settings.connect_timeout_seconds):
                if not connection.is_closed():
                    await connection.execute("ROLLBACK")
                    await connection.close()
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, TimeoutError):
            connection.terminate()
        except BaseException:
            connection.terminate()
            raise

    async def _rights(self, connection: Any) -> RightsReport:
        """Запросы 1–13 §7.6.6 подряд в транзакции обращения; первым — версия сервера.

        Находки — по видам в порядке таблицы §7.6.1. Запрос 8 — с ветвью
        `pg_parameter_acl`, а запрос 10 отправляется только на PostgreSQL 15+.
        """
        parameters = await connection.fetchval(_VERSION) >= _PARAMETER_ACL_VERSION
        findings: list[RightsFinding] = []
        attributes = await connection.fetch(_ATTRIBUTES)
        for kind, column in zip(_ATTRIBUTE_KINDS, _ATTRIBUTE_COLUMNS, strict=True):
            roles = [("role", row["rolname"]) for row in attributes if row[column]]
            if roles:
                findings.append(finding(kind, roles))
        owned = await connection.fetch(_OWNERSHIP)
        if owned:
            findings.append(
                RightsFinding(
                    "owner",
                    (),
                    tuple((row["type"], row["identity"]) for row in owned),
                    owned[0]["total"],
                )
            )
        written = await connection.fetch(_WRITE)
        if written:
            granted = {item for row in written for item in row["privileges"]}
            findings.append(
                RightsFinding(
                    "write",
                    tuple(item for item in _WRITE_ORDER if item in granted),
                    tuple((_RELATION_TYPES[_char(row["relkind"])], row["name"]) for row in written),
                    written[0]["total"],
                )
            )
        created = await connection.fetch(_CREATE)
        if created:
            findings.append(
                finding("create", [(row["type"], row["name"]) for row in created], ["CREATE"])
            )
        predefined = await connection.fetch(_PREDEFINED)
        if predefined:
            findings.append(
                finding("predefined_role", [("role", row["rolname"]) for row in predefined])
            )
        definers = await connection.fetch(_DEFINER)
        warnings = []
        if definers:
            warnings.append(
                RightsFinding(
                    "definer_function",
                    ("EXECUTE",),
                    tuple(("function", row["name"]) for row in definers),
                    definers[0]["total"],
                )
            )
        maintained = await connection.fetch(_MAINTAIN)
        if maintained:
            findings.append(
                RightsFinding(
                    "maintain",
                    ("MAINTAIN",),
                    tuple(
                        (_RELATION_TYPES[_char(row["relkind"])], row["name"]) for row in maintained
                    ),
                    maintained[0]["total"],
                )
            )
        findings.extend(await self._acl_findings(connection, parameters))
        findings.extend(await self._file_findings(connection))
        findings.sort(key=lambda item: FINDING_KINDS.index(item.kind))
        return RightsReport(findings, warnings)

    @staticmethod
    async def _acl_findings(connection: Any, parameters: bool) -> list[RightsFinding]:
        """Запросы 8–10: права с передачей, членство с передачей, права на параметры."""
        findings: list[RightsFinding] = []
        grantable = await connection.fetch(_GRANT_OPTION[parameters])
        if grantable:
            findings.append(
                RightsFinding(
                    "grant_option",
                    tuple(sorted({item for row in grantable for item in row["privileges"]})),
                    tuple((row["type"], row["name"]) for row in grantable),
                    grantable[0]["total"],
                )
            )
        admin = await connection.fetch(_ADMIN_OPTION)
        if admin:
            findings.append(finding("admin_option", [("role", row["rolname"]) for row in admin]))
        if parameters:
            settings = await connection.fetch(_SERVER_SETTINGS)
            if settings:
                granted = {item for row in settings for item in row["privileges"]}
                findings.append(
                    RightsFinding(
                        "server_settings",
                        tuple(item for item in ("ALTER SYSTEM", "SET") if item in granted),
                        tuple(("parameter", row["name"]) for row in settings),
                        settings[0]["total"],
                    )
                )
        return findings

    @staticmethod
    async def _file_findings(connection: Any) -> list[RightsFinding]:
        """Запросы 11–13: закрытые функции, запись в большие объекты (§7.6.6)."""
        findings: list[RightsFinding] = []
        functions = await connection.fetch(_CATALOG_FUNCTION)
        if functions:
            findings.append(
                RightsFinding(
                    "catalog_function",
                    ("EXECUTE",),
                    tuple(("function", row["name"]) for row in functions),
                    functions[0]["total"],
                )
            )
        objects = await connection.fetch(_LARGE_OBJECT)
        if objects:
            findings.append(
                RightsFinding(
                    "large_object",
                    ("UPDATE",),
                    tuple(("large object", row["name"]) for row in objects),
                    objects[0]["total"],
                )
            )
        if await connection.fetchval(_LO_COMPAT):
            findings.append(
                RightsFinding(
                    "lo_compat_privileges", (), (("parameter", "lo_compat_privileges"),), 1
                )
            )
        return findings

    async def _checked(self, connection: Any) -> RightsReport:
        report = await self._rights(connection)
        if report.findings:
            raise DbRightsExcessError(report)
        return report

    async def check(self, target: DbTarget) -> RightsReport:
        """Проверка подключения: права, затем схемы (§7.6.2)."""
        async with self._access(target) as connection:
            report = await self._rights(connection)
            if report.findings:
                return report
            missing = [row["s"] for row in await connection.fetch(_MISSING_SCHEMAS, target.schemas)]
            if missing:
                raise DbUnavailableError("schemas_unavailable", {"schemas": missing}, report)
            return report

    async def snapshot(self, target: DbTarget) -> tuple[RightsReport, SnapshotContent]:
        """Проверка прав и снимок схемы (§7.6.7)."""
        settings = self._settings
        async with self._access(target) as connection:
            report = await self._checked(connection)
            relations = await connection.fetch(
                _TABLES,
                list(target.schemas),
                settings.comment_max_chars,
                settings.snapshot_max_tables + 1,
            )
            truncated = len(relations) > settings.snapshot_max_tables
            relations = relations[: settings.snapshot_max_tables]
            oids = [row["oid"] for row in relations]
            columns: dict[int, list[Any]] = {}
            for row in await connection.fetch(_COLUMNS, oids, settings.comment_max_chars):
                columns.setdefault(row["attrelid"], []).append(row)
            constraints = await connection.fetch(_CONSTRAINTS, oids)
            postgis = await connection.fetchval(_POSTGIS)
        content, cut = _snapshot_tables(
            relations, columns, constraints, postgis, settings.snapshot_max_columns
        )
        return report, SnapshotContent(content, truncated or cut, postgis)

    async def sample(
        self, target: DbTarget, tables: Sequence[SampleRequest], *, rows: int, preview: bool
    ) -> tuple[RightsReport, Sequence[SampleRows]]:
        """Проверка прав и первые строки таблиц; байты считаются по мере чтения (§7.6.8)."""
        settings = self._settings
        result: list[SampleRows] = []
        async with self._access(target) as connection:
            report = await self._checked(connection)
            postgis = await connection.fetchval(_POSTGIS) if preview else None
            size = 0
            stopped = False
            for table in tables:
                if stopped:
                    result.append(SampleRows(table.columns, [], [] if preview else None, True))
                    continue
                geometry = table.geometry if preview and postgis is not None else None
                query = _sample_query(table, geometry, postgis)
                read: list[list[str | None]] = []
                shapes: list[str | None] = []
                async for record in connection.cursor(
                    query,
                    rows,
                    settings.value_max_bytes,
                    prefetch=1,
                    timeout=settings.client_timeout_seconds,
                ):
                    values = list(record.values())
                    size += sum(len(value.encode()) for value in values if value is not None)
                    if size > settings.sample_max_bytes:
                        stopped = True
                        break
                    if geometry is not None:
                        shapes.append(values.pop())
                    read.append(values)
                if preview and geometry is None:
                    shapes = [None] * len(read)
                result.append(SampleRows(table.columns, read, shapes if preview else None, stopped))
        return report, result


def _sample_query(table: SampleRequest, geometry: str | None, postgis: str | None) -> str:
    """Запрос образца §7.6.8; имена уже сверены со снимком вызывающим."""
    cells = [
        f"CASE WHEN pg_catalog.octet_length(t.{ident(name)}::pg_catalog.text) <= $2"
        f" THEN t.{ident(name)}::pg_catalog.text END"
        for name in table.columns
    ]
    source = f"{ident(table.schema)}.{ident(table.table)} AS t"
    if geometry is None or postgis is None:
        return f"SELECT {', '.join(cells) or 'NULL'} FROM {source} LIMIT $1"
    p, g = ident(postgis), ident(geometry)
    shape = f't.{g}::{p}."geometry"'
    lateral = (
        f"CROSS JOIN LATERAL (SELECT CASE WHEN {p}.st_srid({shape}) <> 0"
        f" AND EXISTS (SELECT 1 FROM {p}.spatial_ref_sys WHERE srid = {p}.st_srid({shape}))"
        f" THEN {p}.st_asgeojson({p}.st_transform({shape}, 4326), 7) END AS json) AS x"
    )
    cells.append("CASE WHEN pg_catalog.octet_length(x.json) <= $2 THEN x.json END")
    return f"SELECT {', '.join(cells)} FROM {source} {lateral} LIMIT $1"


def _snapshot_tables(
    relations: Sequence[Any],
    columns: dict[int, list[Any]],
    constraints: Sequence[Any],
    postgis: str | None,
    max_columns: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Таблицы снимка; столбцы сверх предела — таблица и следующие не входят."""
    names = {oid: {row["attnum"]: row["attname"] for row in rows} for oid, rows in columns.items()}
    keys: dict[int, set[int]] = {}
    references: dict[tuple[int, int], dict[str, Any]] = {}
    for row in constraints:
        conkey = list(row["conkey"] or [])
        if _char(row["contype"]) == "p":
            keys.setdefault(row["conrelid"], set()).update(conkey)
            continue
        target_names = names.get(row["ref_oid"], {})
        for own, other in zip(conkey, list(row["confkey"] or []), strict=False):
            references.setdefault(
                (row["conrelid"], own),
                {
                    "schema": row["ref_schema"],
                    "table": row["ref_table"],
                    "column": target_names.get(other),
                },
            )
    tables: list[dict[str, Any]] = []
    total = 0
    for relation in relations:
        described = columns.get(relation["oid"], [])
        total += len(described)
        if total > max_columns:
            return tables, True
        listed = []
        for row in described:
            type_text, geometry, internal, binary = column_types(row, postgis)
            listed.append(
                {
                    "name": row["attname"],
                    "type": type_text,
                    "not_null": row["attnotnull"],
                    "primary_key": row["attnum"] in keys.get(relation["oid"], set()),
                    "references": references.get((relation["oid"], row["attnum"])),
                    "comment": row["comment"],
                    "geometry": geometry,
                    "sandbox_type": internal,
                    "binary": binary,
                }
            )
        tables.append(
            {
                "schema": relation["nspname"],
                "name": relation["relname"],
                "kind": "view" if _char(relation["relkind"]) in ("v", "m") else "table",
                "comment": relation["comment"],
                "columns": listed,
            }
        )
    return tables, False
