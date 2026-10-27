"""Сущности подключений к базам заказчика и правила снимка схемы (§7.6.1, §7.6.7, §7.6.9).

Имена схем, таблиц, столбцов и комментарии снимка — недоверенный текст из базы заказчика:
модели они уходят только блоком данных (`data_block`), в SQL — только идентификаторами в
кавычках после сверки со снимком.
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

TlsMode = Literal["verify", "require", "disable"]
RightsStatus = Literal["unknown", "read_only", "excess"]
Purpose = Literal["check", "snapshot", "sample_check", "sample_preview"]

# Порядок видов находок и предупреждений — таблица §7.6.1.
FINDING_KINDS = (
    "superuser",
    "createrole",
    "createdb",
    "replication",
    "bypassrls",
    "owner",
    "write",
    "maintain",
    "create",
    "grant_option",
    "predefined_role",
    "admin_option",
    "server_settings",
    "catalog_function",
    "large_object",
    "lo_compat_privileges",
    "definer_function",
)
_OBJECTS_SHOWN = 3
_NAME_MAX_CHARS = 300

# Слова, которые PostgreSQL не принимает именем без кавычек (зарезервированные и
# «могут быть именем функции или типа»): `quote_ident` берёт такие имена в кавычки.
_KEYWORDS = frozenset(
    """all analyse analyze and any array as asc asymmetric authorization between bigint binary
    bit boolean both case cast char character check coalesce collate collation column
    concurrently constraint create cross current_catalog current_date current_role
    current_schema current_time current_timestamp current_user dec decimal default
    deferrable desc distinct do else end except exists extract false fetch float for foreign
    freeze from full grant greatest group grouping having ilike in initially inner inout int
    integer intersect interval into is isnull join json json_array json_arrayagg json_exists
    json_object json_objectagg json_query json_scalar json_serialize json_table json_value
    lateral leading least left like limit localtime localtimestamp merge_action national
    natural nchar none normalize not notnull null nullif numeric offset on only or order out
    outer overlaps overlay placing position precision primary real references returning
    right row select session_user setof similar smallint some substring symmetric
    system_user table tablesample then time timestamp to trailing treat trim true union
    unique user using values varchar variadic verbose when where window with xmlattributes
    xmlconcat xmlelement xmlexists xmlforest xmlnamespaces xmlparse xmlpi xmlroot
    xmlserialize xmltable""".split()  # noqa: SIM905
)
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_$]*")
_SINGLE_LINE = re.compile(r"\s+")


@dataclass(frozen=True)
class RightsFinding:
    """Вид лишнего права или предупреждения: первые три объекта по имени и общее число."""

    kind: str
    privileges: tuple[str, ...]
    objects: tuple[tuple[str, str], ...]
    total: int

    def data(self) -> dict[str, Any]:
        """Объект `RightsFinding` контракта; имена — не длиннее 300 символов."""
        return {
            "kind": self.kind,
            "privileges": list(self.privileges),
            "objects": [
                {"type": kind, "name": name[:_NAME_MAX_CHARS]} for kind, name in self.objects
            ],
            "total": self.total,
        }


def finding(
    kind: str, objects: Sequence[tuple[str, str]], privileges: Sequence[str] = ()
) -> RightsFinding:
    """Находка по всем найденным объектам: первые три по имени, `total` — все."""
    ordered = sorted(objects, key=lambda item: item[1])
    return RightsFinding(kind, tuple(privileges), tuple(ordered[:_OBJECTS_SHOWN]), len(objects))


@dataclass
class Connection:
    """Строка `db_connections`; пароль — только шифртекст и не попадает в `repr`."""

    id: UUID
    title: str
    description: str
    host: str
    port: int
    database: str
    username: str
    password_encrypted: bytes = field(repr=False)
    tls_mode: TlsMode
    schemas: list[str]
    samples_allowed: bool
    sample_exclusions: list[dict[str, str | None]]
    access_all: bool
    config_changed_at: datetime
    rights_status: RightsStatus
    rights_findings: list[dict[str, Any]]
    rights_warnings: list[dict[str, Any]]
    rights_checked_at: datetime | None
    last_error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AccessUser:
    """Сотрудник из списка доступа к подключению."""

    id: UUID
    login: str
    full_name: str


@dataclass(frozen=True)
class SnapshotSummary:
    """Сводка снимка без чтения `content` — для списков."""

    taken_at: datetime
    tables: int
    truncated: bool


@dataclass(frozen=True)
class StoredSnapshot:
    """Строка `db_snapshots`: таблицы с внутренними `sandbox_type` и `binary` у столбцов."""

    connection_id: UUID
    taken_at: datetime
    tables: list[dict[str, Any]]
    truncated: bool
    postgis_schema: str | None

    @property
    def summary(self) -> SnapshotSummary:
        """Сводка снимка."""
        return SnapshotSummary(self.taken_at, len(self.tables), self.truncated)


@dataclass(frozen=True)
class Selection:
    """Выбор подключения у диалога или проекта (§7.6.9): название на момент выбора."""

    connection_id: UUID | None
    title: str
    tables: tuple[tuple[str, str], ...]

    def tables_data(self) -> list[dict[str, str]]:
        """Таблицы выбора в виде `[{schema, name}]`."""
        return [{"schema": schema, "name": name} for schema, name in self.tables]


def selection_of(
    connection_id: UUID | None, title: str | None, tables: Iterable[Mapping[str, str]]
) -> Selection | None:
    """Выбор из полей строки; `title = None` — выбора нет."""
    if title is None:
        return None
    return Selection(connection_id, title, tuple((item["schema"], item["name"]) for item in tables))


def refresh_available_at(
    connection: Connection, snapshot: SnapshotSummary | None, minutes: int
) -> datetime | None:
    """С какого времени можно обновить снимок; `None` — уже можно (§7.6.3)."""
    if snapshot is None or connection.config_changed_at > snapshot.taken_at:
        return None
    return snapshot.taken_at + timedelta(minutes=minutes)


ExclusionMap = Mapping[tuple[str, str], frozenset[str] | None]


def exclusion_map(exclusions: Sequence[Mapping[str, str | None]]) -> ExclusionMap:
    """Исключения образцов словарём «(схема, таблица) → вся таблица (`None`) или столбцы».

    Строится один раз на ответ, проверку или слой (§7.6.1): поиск таблицы и столбца — по
    словарю, а не перебором списка (столбцы снимка × исключения).
    """
    columns: dict[tuple[str, str], set[str]] = {}
    whole: set[tuple[str, str]] = set()
    for item in exclusions:
        key = (str(item["schema"]), str(item["table"]))
        column = item.get("column")
        if column is None:
            whole.add(key)
        else:
            columns.setdefault(key, set()).add(column)
    result: dict[tuple[str, str], frozenset[str] | None] = {
        key: frozenset(names) for key, names in columns.items()
    }
    result.update(dict.fromkeys(whole))
    return result


def table_excluded(exclusions: ExclusionMap, schema: str, table: str) -> bool:
    """Таблица исключена из образцов целиком."""
    key = (schema, table)
    return key in exclusions and exclusions[key] is None


def column_excluded(exclusions: ExclusionMap, schema: str, table: str, column: str) -> bool:
    """Столбец исключён из образцов (сам или вместе с таблицей)."""
    key = (schema, table)
    if key not in exclusions:
        return False
    names = exclusions[key]
    return names is None or column in names


def visible_tables(connection: Connection, snapshot: StoredSnapshot) -> list[dict[str, Any]]:
    """Таблицы снимка схем подключения в порядке `schemas` (снятые схемы не отдаются)."""
    order = {schema: position for position, schema in enumerate(connection.schemas)}
    tables = [item for item in snapshot.tables if item["schema"] in order]
    return sorted(tables, key=lambda item: (order[item["schema"]], item["name"]))


def snapshot_body(connection: Connection, snapshot: StoredSnapshot) -> dict[str, Any]:
    """Объект `DbSnapshot` (§7.6.1): исключения — по текущим `sample_exclusions`."""
    exclusions = exclusion_map(connection.sample_exclusions)
    tables = []
    for table in visible_tables(connection, snapshot):
        schema, name = table["schema"], table["name"]
        tables.append(
            {
                "schema": schema,
                "name": name,
                "kind": table["kind"],
                "comment": table["comment"],
                "samples_excluded": table_excluded(exclusions, schema, name),
                "columns": [
                    {
                        "name": column["name"],
                        "type": column["type"],
                        "not_null": column["not_null"],
                        "primary_key": column["primary_key"],
                        "references": column["references"],
                        "comment": column["comment"],
                        "geometry": column["geometry"],
                        "excluded": column_excluded(exclusions, schema, name, column["name"]),
                    }
                    for column in table["columns"]
                ],
            }
        )
    return {
        "connection_id": str(connection.id),
        "taken_at": snapshot.taken_at,
        "truncated": snapshot.truncated,
        "tables": tables,
    }


TableIndex = dict[tuple[str, str], dict[str, Any]]


def table_index(connection: Connection, snapshot: StoredSnapshot) -> TableIndex:
    """Таблицы снимка по `(схема, имя)` — только среди схем подключения.

    Строится один раз на проверку: поиск каждой таблицы запроса — по словарю, а не
    перебором снимка (запрос сотрудника может назвать тысячи таблиц).
    """
    schemas = set(connection.schemas)
    return {
        (item["schema"], item["name"]): item
        for item in snapshot.tables
        if item["schema"] in schemas
    }


def find_table(
    connection: Connection, snapshot: StoredSnapshot, schema: str, name: str
) -> dict[str, Any] | None:
    """Таблица снимка по точному имени — только среди схем подключения."""
    if schema not in connection.schemas:
        return None
    return next(
        (item for item in snapshot.tables if item["schema"] == schema and item["name"] == name),
        None,
    )


@dataclass(frozen=True)
class CopyPlan:
    """Какие столбцы таблицы копируются в образец, а какие остаются пустыми (§7.6.8)."""

    columns: tuple[str, ...]
    excluded: tuple[tuple[str, Literal["excluded", "binary"]], ...]


def copy_plan(
    connection: Connection, table: Mapping[str, Any], exclusions: ExclusionMap | None = None
) -> CopyPlan:
    """Столбцы для выборки: кроме исключённых администратором и двоичных.

    `exclusions` — словарь исключений подключения, если вызывающий уже построил его.
    """
    if exclusions is None:
        exclusions = exclusion_map(connection.sample_exclusions)
    schema, name = table["schema"], table["name"]
    copied: list[str] = []
    excluded: list[tuple[str, Literal["excluded", "binary"]]] = []
    for column in table["columns"]:
        if column_excluded(exclusions, schema, name, column["name"]):
            excluded.append((column["name"], "excluded"))
        elif column["binary"]:
            excluded.append((column["name"], "binary"))
        else:
            copied.append(column["name"])
    return CopyPlan(tuple(copied), tuple(excluded))


def quote_ident(name: str) -> str:
    """Имя по правилам `quote_ident` PostgreSQL: в кавычках, если без них оно иное."""
    if _PLAIN_NAME.fullmatch(name) and name not in _KEYWORDS:
        return name
    return '"' + name.replace('"', '""') + '"'


def _one_line(text: str) -> str:
    return _SINGLE_LINE.sub(" ", text).strip()


def ddl_text(tables: Sequence[Mapping[str, Any]]) -> str:
    """DDL-подобный текст отмеченных таблиц для модели (§7.6.9)."""
    parts = []
    for table in tables:
        lines = []
        header = []
        if table["kind"] == "view":
            header.append("представление;")
        if table.get("comment"):
            header.append(_one_line(table["comment"]))
        if header:
            lines.append("-- " + " ".join(header))
        lines.append(f"CREATE TABLE {quote_ident(table['schema'])}.{quote_ident(table['name'])} (")
        columns = table["columns"]
        for position, column in enumerate(columns):
            line = f"  {quote_ident(column['name'])} {column['type']}"
            if column["not_null"]:
                line += " NOT NULL"
            if column["primary_key"]:
                line += " PRIMARY KEY"
            reference = column["references"]
            if reference is not None:
                line += (
                    f" REFERENCES {quote_ident(reference['schema'])}."
                    f"{quote_ident(reference['table'])}"
                )
                # Столбец таблицы вне снимка неизвестен (`column: null`) — без скобок.
                if reference["column"] is not None:
                    line += f"({quote_ident(reference['column'])})"
            if position < len(columns) - 1:
                line += ","
            if column.get("comment"):
                line += f"  -- {_one_line(column['comment'])}"
            lines.append(line)
        lines.append(");")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def selected_tables(
    connection: Connection, snapshot: StoredSnapshot, selection: Selection
) -> list[dict[str, Any]]:
    """Таблицы выбора, которые есть в текущем снимке, в порядке выбора."""
    index = table_index(connection, snapshot)
    found = []
    for key in selection.tables:
        table = index.get(key)
        if table is not None:
            found.append(table)
    return found


def resolve_table(
    connection: Connection, index: TableIndex, schema: str | None, name: str
) -> dict[str, Any] | None:
    """Таблица запроса в снимке (§7.4, «Проверка на образце», шаг 1).

    Имя со схемой — точно; без схемы — по порядку `schemas` подключения. Имена уже
    приведены по правилам PostgreSQL (без кавычек — в нижнем регистре).
    """
    if schema is not None:
        return index.get((schema, name))
    for candidate in connection.schemas:
        found = index.get((candidate, name))
        if found is not None:
            return found
    return None


InventedReason = Literal["not_in_snapshot", "excluded", "samples_not_allowed", "table_limit"]


def sample_origin(
    connection: Connection, table: Mapping[str, Any], plan: CopyPlan | None = None
) -> InventedReason | None:
    """`None` — таблица идёт образцом; иначе причина, по которой данные придумает модель.

    Таблица, исключённая целиком, не даёт столбцов для копирования; `plan` — уже
    посчитанный `copy_plan` этой таблицы.
    """
    if not connection.samples_allowed:
        return "samples_not_allowed"
    if not (plan if plan is not None else copy_plan(connection, table)).columns:
        return "excluded"
    return None
