/**
 * Выбор базы для помощника (концепция §5.7, §5.13.23): список «Схема базы» с группами «Мои схемы» и
 * «Базы данных», значения списка и даты снимка. Названия и описания подключений — текст администратора,
 * выводятся как текст.
 */
import { MenuHeader, MenuItem, Select } from '@skbkontur/react-ui';

import type { DbConnection, DbTableRef, SqlSchemaSummary } from '../api/types';
import { texts } from '../texts';
import styles from './selection.module.css';

/** «Без схемы» в списке. */
export const NO_SCHEMA = '';
const DB_PREFIX = 'db:';

/** Значение списка для подключения; у схемы из «Мои схемы» значение — её идентификатор. */
export function dbValue(connectionId: string): string {
  return `${DB_PREFIX}${connectionId}`;
}

/** Идентификатор подключения из значения списка; `null` — схема или «Без схемы». */
export function connectionOf(value: string | null): string | null {
  return value?.startsWith(DB_PREFIX) ? value.slice(DB_PREFIX.length) : null;
}

export function tableName(table: DbTableRef): string {
  return `${table.schema}.${table.name}`;
}

type SelectItems = React.ComponentProps<typeof Select<string, string>>['items'];

/**
 * Пункты списка «Схема базы»: «Без схемы», группа «Мои схемы», группа «Базы данных» (ПД §5.8). Группа без пунктов
 * не показывается. Подключение, которое временно не используется, — недоступный пункт с пояснением.
 * `title` — название поля первой строкой раскрытого списка (у списка без подписи в строке заголовка).
 */
export function schemaItems(schemas: SqlSchemaSummary[], connections: DbConnection[], title?: string): SelectItems {
  const d = texts.db;
  const items: NonNullable<SelectItems> = [];
  if (title) {
    items.push(Select.staticElement(() => <MenuHeader>{title}</MenuHeader>));
  }
  items.push([NO_SCHEMA, texts.sql.noSchema]);
  if (schemas.length > 0) {
    items.push(Select.SEP, Select.staticElement(() => <MenuHeader>{d.groupSchemas}</MenuHeader>));
    items.push(...schemas.map((schema): [string, string] => [schema.id, schema.name]));
  }
  if (connections.length > 0) {
    items.push(Select.SEP, Select.staticElement(() => <MenuHeader>{d.groupDatabases}</MenuHeader>));
    for (const connection of connections) {
      if (connection.usable) {
        items.push([dbValue(connection.id), connection.title, <Description key="d" text={connection.description} />]);
      } else {
        items.push(
          Select.staticElement(() => (
            <MenuItem disabled comment={<Description text={d.unusableItem} />}>
              {connection.title}
            </MenuItem>
          )),
        );
      }
    }
  }
  return items;
}

function Description({ text }: { text: string }) {
  return text ? <span className={styles.description}>{text}</span> : null;
}

/** Подпись выбранного значения: подключение может быть недоступным пунктом — его ищем сами. */
export function selectionLabel(value: string, schemas: SqlSchemaSummary[], connections: DbConnection[]): string {
  const connectionId = connectionOf(value);
  if (connectionId) {
    return connections.find((item) => item.id === connectionId)?.title ?? texts.sql.noSchema;
  }
  return schemas.find((item) => item.id === value)?.name ?? texts.sql.noSchema;
}

const pad = (value: number) => String(value).padStart(2, '0');
const sameDay = (date: Date, now: Date) => date.toDateString() === now.toDateString();
const clock = (date: Date) => `${pad(date.getHours())}:${pad(date.getMinutes())}`;
const day = (date: Date) => `${pad(date.getDate())}.${pad(date.getMonth() + 1)}.${date.getFullYear()}`;

/** «25.10.2026, 14:05» — дата снимка в шапке выбора таблиц (§5.7). */
export function snapshotDate(iso: string): string {
  const date = new Date(iso);
  return `${day(date)}, ${clock(date)}`;
}

/** «25.10.2026» — дата снимка в заметках о подключении, которое не используется. */
export function snapshotDay(iso: string): string {
  return day(new Date(iso));
}

/** «сегодня в 14:05» или «25.10.2026 в 14:05» — когда обновляли схему. */
export function refreshedAt(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  return sameDay(date, now) ? `сегодня в ${clock(date)}` : `${day(date)} в ${clock(date)}`;
}

/** «14:35» или «26.10.2026, 14:35» — с какого времени можно обновить снова. */
export function availableAt(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  return sameDay(date, now) ? clock(date) : `${day(date)}, ${clock(date)}`;
}
