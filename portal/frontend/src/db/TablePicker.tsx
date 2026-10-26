import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { Button, Checkbox, Input, Link, Loader, SidePage, SingleToast, Spinner } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { DbConnection, DbSnapshot, DbTable, DbTableRef } from '../api/types';
import { Notice } from '../components/Notice';
import { TitleHint } from '../components/TitleHint';
import { texts } from '../texts';
import { dbRefusal } from './refusal';
import { availableAt, refreshedAt, snapshotDate, tableName } from './selection';
import styles from './TablePicker.module.css';

const d = texts.db;
const p = d.picker;
/** Сколько имён исчезнувших таблиц в заметке, дальше — «и ещё N» (§5.7). */
const GONE_SHOWN = 5;

function goneText(names: string[]): string {
  const shown = names.slice(0, GONE_SHOWN).join(', ');
  return p.gone(names.length > GONE_SHOWN ? `${shown} ${p.andMore(names.length - GONE_SHOWN)}` : shown);
}

const sameTable = (a: DbTableRef, b: DbTableRef) => a.schema === b.schema && a.name === b.name;

type Load = { status: 'loading' } | { status: 'missing' } | { status: 'failed' } | { status: 'ready'; snapshot: DbSnapshot };

export interface TablePickerProps {
  connection: DbConnection;
  selected: DbTableRef[];
  /** Предел отметок (`db.max_selected_tables`). */
  max: number;
  onChange: (tables: DbTableRef[]) => void;
  /** Схема загружена впервые или обновлена: выбор с этим подключением теперь можно сохранить. */
  onSnapshot?: (snapshot: DbSnapshot) => void;
  /** Подключения выключены в настройках портала (`403 feature_disabled`). */
  onFeatureOff: () => void;
  /** Меняется — схема перечитывается (таблицы нет в схеме: её обновили в другой вкладке). */
  reloadKey?: number;
  /** Отметки сохраняются дольше 300 мс — «Сохраняю отметки…» в подвале. */
  saving?: boolean;
  /** Справа в подвале: уровень памяти диалога (SQL-помощник). */
  footerExtra?: React.ReactNode;
  /** Над подвалом: заметка о переполненной памяти. */
  overflow?: boolean;
  /** Встроен в настройки проекта: список — в области до 320 px, без шапки окна (§5.13.23). */
  embedded?: boolean;
}

/**
 * Выбор таблиц подключения (свой: `TablePicker`, концепция §5.7): шапка, список и подвал — отдельно, чтобы в
 * боковой панели шапка и подвал стояли на месте. Снимок приходит целиком; имена и комментарии из базы —
 * недоверенные данные: только текст, без разметки и ссылок.
 */
function usePickerParts(props: TablePickerProps) {
  const { connection, selected, max, onChange, onSnapshot, onFeatureOff, reloadKey = 0, embedded = false } = props;
  const [load, setLoad] = useState<Load>({ status: 'loading' });
  const [refreshing, setRefreshing] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [gone, setGone] = useState<string[]>([]);
  const [query, setQuery] = useState('');
  const [onlyChecked, setOnlyChecked] = useState(false);
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const [refreshFrom, setRefreshFrom] = useState(connection.refresh_available_at);
  const [attempt, setAttempt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  // Обновить снова можно с указанного времени: к нему кнопка становится доступной сама.
  useEffect(() => {
    if (!refreshFrom) {
      return;
    }
    const wait = new Date(refreshFrom).getTime() - Date.now();
    const timer = window.setTimeout(() => setNow(Date.now()), Math.max(0, wait) + 500);
    return () => window.clearTimeout(timer);
  }, [refreshFrom]);
  const latest = useRef({ selected, onChange, onSnapshot, onFeatureOff });
  useEffect(() => {
    latest.current = { selected, onChange, onSnapshot, onFeatureOff };
  });

  /** Отметки с таблиц, которых в схеме нет, снимаются — с заметкой (§5.7). */
  const accept = useCallback((snapshot: DbSnapshot) => {
    const { selected: current, onChange: change } = latest.current;
    const missing = current.filter((table) => !snapshot.tables.some((item) => sameTable(item, table)));
    if (missing.length > 0) {
      setGone(missing.map(tableName));
      change(current.filter((table) => !missing.includes(table)));
    }
    setLoad({ status: 'ready', snapshot });
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.getDbSnapshot(connection.id).then(
      (snapshot) => {
        if (!cancelled) {
          accept(snapshot);
        }
      },
      (error: unknown) => {
        if (cancelled) {
          return;
        }
        if (isApiError(error, 'db_snapshot_missing')) {
          setLoad({ status: 'missing' });
        } else if (isApiError(error, 'feature_disabled')) {
          latest.current.onFeatureOff();
        } else if (isApiError(error, 'not_found')) {
          setRefusal(d.refusals.unavailable);
          setLoad({ status: 'failed' });
        } else {
          setLoad({ status: 'failed' });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [accept, attempt, connection.id, reloadKey]);

  const refresh = async () => {
    setRefreshing(true);
    setRefusal(null);
    try {
      const snapshot = await api.refreshDbSnapshot(connection.id);
      SingleToast.push(p.refreshed(snapshot.tables.length));
      setRefreshFrom(null);
      accept(snapshot);
      latest.current.onSnapshot?.(snapshot);
    } catch (error) {
      if (isApiError(error, 'db_snapshot_too_soon')) {
        const at = typeof error.details.available_at === 'string' ? error.details.available_at : null;
        setRefreshFrom(at);
        if (at && load.status === 'ready') {
          SingleToast.push(p.tooSoon(refreshedAt(load.snapshot.taken_at), availableAt(at)));
        }
      } else if (isApiError(error, 'feature_disabled')) {
        latest.current.onFeatureOff();
      } else if (isApiError(error, 'not_found')) {
        setRefusal(d.refusals.unavailable);
      } else if (!isApiError(error, 'unauthenticated')) {
        setRefusal(dbRefusal(error) ?? texts.common.actionFailed);
      }
    } finally {
      setRefreshing(false);
    }
  };

  const snapshot = load.status === 'ready' ? load.snapshot : null;
  const tooSoon = snapshot && refreshFrom && new Date(refreshFrom).getTime() > now ? refreshFrom : null;
  const refreshButton = tooSoon ? (
    <TitleHint text={p.tooSoon(refreshedAt(snapshot?.taken_at ?? tooSoon), availableAt(tooSoon))} always>
      <Button size="small" aria-disabled>
        {p.refresh}
      </Button>
    </TitleHint>
  ) : (
    <Button size="small" loading={refreshing} onClick={() => void refresh()}>
      {p.refresh}
    </Button>
  );

  const toggle = (table: DbTable, checked: boolean) => {
    const ref = { schema: table.schema, name: table.name };
    onChange(checked ? [...selected, ref] : selected.filter((item) => !sameTable(item, ref)));
  };

  const isChecked = (table: DbTable) => selected.some((item) => sameTable(item, table));
  const needle = query.trim().toLowerCase();
  const visible = (snapshot?.tables ?? []).filter(
    (table) =>
      (!onlyChecked || isChecked(table)) &&
      (!needle || table.name.toLowerCase().includes(needle) || tableName(table).toLowerCase().includes(needle)),
  );
  const groups = new Map<string, DbTable[]>();
  for (const table of [...visible].sort((a, b) => a.schema.localeCompare(b.schema) || a.name.localeCompare(b.name))) {
    groups.set(table.schema, [...(groups.get(table.schema) ?? []), table]);
  }
  const full = selected.length >= max;

  let list: React.ReactNode;
  if (load.status === 'loading') {
    list = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.placeholder} />
      </Loader>
    );
  } else if (load.status === 'missing') {
    list = (
      <div className={styles.missing}>
        <p>{p.noSnapshot}</p>
        <div>
          <Button use="primary" loading={refreshing} onClick={() => void refresh()}>
            {p.loadSnapshot}
          </Button>
        </div>
      </div>
    );
  } else if (load.status === 'failed') {
    list = refusal ? null : (
      <Notice
        kind="error"
        action={{
          label: texts.common.retry,
          onClick: () => {
            setLoad({ status: 'loading' });
            setAttempt((value) => value + 1);
          },
        }}
      >
        {p.loadFailed}
      </Notice>
    );
  } else if (visible.length === 0) {
    list = <p className={styles.muted}>{p.nothingFound}</p>;
  } else {
    list = [...groups].map(([schema, tables]) => (
      <fieldset key={schema} className={styles.group}>
        <legend className={styles.schema}>{schema}</legend>
        {tables.map((table) => {
          const key = tableName(table);
          const expanded = open.has(key);
          return (
            <div key={key} className={styles.table}>
              <div className={styles.row}>
                <Checkbox
                  checked={isChecked(table)}
                  disabled={full && !isChecked(table)}
                  onValueChange={(checked) => toggle(table, checked)}
                >
                  <span className={styles.name}>{table.name}</span>
                </Checkbox>
                <TitleHint text={table.comment ?? ''} className={styles.comment}>
                  <span className={styles.ellipsis}>{table.comment ?? (table.kind === 'view' ? p.view : '')}</span>
                </TitleHint>
                <span className={styles.meta}>
                  {p.columns(table.columns.length)}
                  {table.columns.some((column) => column.geometry) && ` · ${p.geometry}`}
                  {table.samples_excluded && (
                    <>
                      {' · '}
                      <TitleHint text={p.noSampleHint} always>
                        <span tabIndex={0}>{p.noSample}</span>
                      </TitleHint>
                    </>
                  )}
                </span>
                <Button
                  use="text"
                  size="small"
                  aria-label={p.showColumns(key)}
                  aria-expanded={expanded}
                  icon={expanded ? <IconArrowCDownRegular16 /> : <IconArrowCRightRegular16 />}
                  onClick={() =>
                    setOpen((current) => {
                      const next = new Set(current);
                      if (expanded) {
                        next.delete(key);
                      } else {
                        next.add(key);
                      }
                      return next;
                    })
                  }
                />
              </div>
              {expanded && (
                <ul className={styles.columns}>
                  {table.columns.map((column) => (
                    <li key={column.name} className={styles.column}>
                      <span className={styles.name}>{column.name}</span>
                      <span>
                        {column.geometry ? `${column.geometry.type}, ${column.geometry.srid}` : column.type}
                        {column.primary_key && ` · ${p.key}`}
                        {column.references &&
                          ` · → ${column.references.table}.${column.references.column}`}
                      </span>
                      {column.comment && <span className={styles.muted}>{column.comment}</span>}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          );
        })}
      </fieldset>
    ));
  }

  const head = (
    <div className={styles.head}>
      {snapshot && (
        <div className={styles.origin}>
          <span className={styles.muted}>{d.snapshot(snapshotDate(snapshot.taken_at), snapshot.tables.length)}</span>
          {refreshButton}
          {refreshing && <span className={styles.muted}>{p.refreshing}</span>}
        </div>
      )}
      <p className={styles.muted}>{d.modelNote}</p>
      {refusal && <Notice kind="error">{refusal}</Notice>}
      {gone.length > 0 && <Notice kind="info">{goneText(gone)}</Notice>}
      {snapshot && (
        <div className={styles.search}>
          <Input width={280} placeholder={p.search} aria-label={p.search} value={query} onValueChange={setQuery} />
          <Checkbox checked={onlyChecked} onValueChange={setOnlyChecked}>
            {p.onlyChecked}
          </Checkbox>
        </div>
      )}
    </div>
  );

  const footer = snapshot && (
    <>
      {props.overflow && <Notice kind="warning">{p.overflow}</Notice>}
      <div className={styles.footer}>
        <span>
          <span>{p.checked(selected.length, snapshot.tables.length)}</span>
          {selected.length > 0 && (
            <>
              {' · '}
              <Link component="button" onClick={() => onChange([])}>
                {p.clear}
              </Link>
            </>
          )}
        </span>
        {props.saving && <Spinner type="mini" caption={p.saving} />}
        {props.footerExtra && <span className={styles.extra}>{props.footerExtra}</span>}
      </div>
      {full && <p className={styles.muted}>{p.limit(max)}</p>}
    </>
  );

  return { head, list: <div className={embedded ? styles.embeddedList : styles.list}>{list}</div>, footer };
}

/** Выбор таблиц, встроенный в настройки проекта: без шапки окна и «Закрыть», список — до 320 px (§5.13.23). */
export function TablePickerBody(props: TablePickerProps) {
  const { head, list, footer } = usePickerParts({ ...props, embedded: true });
  return (
    <div className={styles.picker}>
      {head}
      {list}
      {footer}
    </div>
  );
}

interface SideProps extends TablePickerProps {
  onClose: () => void;
}

/** Выбор таблиц в SQL-помощнике — `SidePage` 640 px, блокирующий, как «Мои схемы» (§5.7). */
export function TablePickerSide({ onClose, ...props }: SideProps) {
  const { head, list, footer } = usePickerParts(props);
  return (
    <SidePage width={640} blockBackground ignoreOutsideClick onClose={onClose}>
      <SidePage.Header>
        {p.title}
        <div className={styles.connection}>
          <h3 className={styles.connectionTitle}>{props.connection.title}</h3>
          {props.connection.description && <p className={styles.muted}>{props.connection.description}</p>}
        </div>
        {head}
      </SidePage.Header>
      <SidePage.Body>
        <SidePage.Container>{list}</SidePage.Container>
      </SidePage.Body>
      <SidePage.Footer panel>
        <div className={styles.sideFooter}>
          {footer}
          <div>
            <Button onClick={onClose}>{p.close}</Button>
          </div>
        </div>
      </SidePage.Footer>
    </SidePage>
  );
}
