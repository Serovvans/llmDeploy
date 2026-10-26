import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Spinner } from '@skbkontur/react-ui';
import { useId, useState } from 'react';

import type { SqlRun, SqlRunBlock, SqlRunRows } from '../../api/types';
import { texts } from '../../texts';
import { TitleHint } from '../TitleHint';
import { CodeBlock } from './CodeBlock';
import styles from './SqlRunResult.module.css';

const t = texts.sql.run;
const d = texts.db.run;

/** Проверка шла на образце из базы (1.0): у блока есть подключение и хоть одна таблица образца (контракт §7.4). */
function isSample(block: SqlRunBlock): boolean {
  return Boolean(block.db) && (block.fixture ?? []).some((table) => table.origin === 'sample');
}

/** Сохранённый блок проверки на образце: строк базы нет, только итог (концепция §5.7, API-52). */
function isSaved(block: SqlRunBlock): boolean {
  return (block.result !== null && block.result.rows === null) || (block.fixture ?? []).some((table) => table.rows === null);
}

function rowCount(result: SqlRunBlock['result']): number {
  return result?.rows?.length ?? result?.row_count ?? 0;
}

/** Имена через запятую и «и», больше трёх — «и ещё ‹N›» (§5.7). */
function names(items: readonly string[]): string {
  if (items.length > 3) {
    return `${items.slice(0, 3).join(', ')} ${d.andMore(items.length - 3)}`;
  }
  return items.length > 1 ? `${items.slice(0, -1).join(', ')} и ${items.at(-1) ?? ''}` : (items[0] ?? '');
}

/** Чем закончился выполнившийся запрос: показал строки или изменения либо данные не покрыли его условия. */
type Outcome = 'rows' | 'affected' | 'emptyRead' | 'emptyWrite';

function outcomeOf(block: SqlRunBlock): Outcome {
  if (block.affected_rows !== null) {
    return block.affected_rows > 0 ? 'affected' : 'emptyWrite';
  }
  return rowCount(block.result) > 0 ? 'rows' : 'emptyRead';
}

function notCheckedText(block: SqlRunBlock): string {
  return (block.status === 'not_checked' && (t.reasons[block.reason ?? ''] ?? d.reasons[block.reason ?? ''])) || t.notChecked;
}

/** «Проверка недоступна» — с причиной подключения у проверки на образце (§5.7). */
function unavailableText(block: SqlRunBlock): string {
  if (!block.db) {
    return t.unavailable;
  }
  return d.reasons[block.reason ?? ''] ?? d.unavailable;
}

/** Что сказать экранному чтению об итоге одного блока (концепция §5.7); у блока без итога — ничего. */
function announcement(block: SqlRunBlock): string | null {
  switch (block.status) {
    case 'pending':
    case 'running':
      return null;
    case 'passed': {
      const outcome = outcomeOf(block);
      if (outcome === 'rows') {
        return isSample(block) ? d.announce.rows(rowCount(block.result)) : t.announce.rows(rowCount(block.result));
      }
      return outcome === 'affected' ? t.announce.affected(block.affected_rows ?? 0) : t.announce[outcome];
    }
    case 'fixed':
      return t.announce.fixed;
    case 'failed':
      return isSample(block) ? d.announce.failed : t.announce.failed;
    case 'unavailable':
      return unavailableText(block);
    case 'timeout':
      return t.timeout;
    case 'stopped':
      return t.stopped;
    default:
      return notCheckedText(block);
  }
}

/** Одно объявление об итогах проверки ответа; шаги проверки не озвучиваются. */
export function sqlRunAnnouncement(run: SqlRun): string {
  return run.blocks.flatMap((block) => announcement(block) ?? []).join('. ');
}

/**
 * Таблица значений из тестовой базы. Значения — недоверенные данные: выводятся как текст;
 * длинные обрезает сервер, интерфейс их не трогает.
 */
function RowsTable({ data, caption }: { data: SqlRunRows; caption: string }) {
  return (
    <>
      <div className={styles.scroll} tabIndex={0} role="region" aria-label={caption}>
        <table className={styles.table}>
          <caption className="p-visually-hidden">{caption}</caption>
          <thead>
            <tr>
              {data.columns.map((column, index) => (
                <th key={index} scope="col">
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(data.rows ?? []).map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, index) => (
                  <td key={index}>{cell ?? <span className={styles.null}>{t.null}</span>}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.truncated && <p className={styles.muted}>{t.truncated(data.rows?.length ?? 0)}</p>}
    </>
  );
}

/**
 * «Тестовые данные · 2 таблицы»: свёрнуты по умолчанию — сначала нужен итог (концепция §5.7). На образце —
 * «Образец · 2 таблицы»; если часть таблиц придумана — «Данные · 3 таблицы» с подписью у каждой (1.0).
 */
function Fixture({ tables, sample }: { tables: NonNullable<SqlRunBlock['fixture']>; sample: boolean }) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  const Arrow = open ? IconArrowCDownRegular16 : IconArrowCRightRegular16;
  const mixed = sample && tables.some((table) => table.origin !== 'sample');
  const label = !sample ? t.fixture(tables.length) : mixed ? d.mixed(tables.length) : d.sample(tables.length);
  return (
    <div>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        <Arrow aria-hidden="true" />
        <span>{label}</span>
      </button>
      {open && (
        <div id={contentId} className={styles.fixture}>
          {tables.map((table, index) => (
            <div key={index} className={styles.part}>
              <p className={`p-mono ${styles.tableName}`}>
                {table.table}
                {mixed && <span className={styles.muted}> · {table.origin === 'sample' ? d.fromSample : d.invented}</span>}
              </p>
              <RowsTable data={table} caption={t.fixtureCaption(table.table)} />
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** Строка происхождения данных проверки с подключением (§5.7, API-52): что взято из базы, что придумано. */
function Origin({ block }: { block: SqlRunBlock }) {
  const db = block.db;
  if (!db) {
    return null;
  }
  const tables = block.fixture ?? [];
  const byReason = (reason: string) => tables.filter((table) => table.origin !== 'sample' && table.invented_reason === reason).map((table) => table.table);
  const sampled = tables.filter((table) => table.origin === 'sample').map((table) => table.table);
  const lines: string[] = [];
  if (sampled.length > 0) {
    lines.push(d.origin(db.connection_title, db.sample_rows, names(sampled), sampled.length > 1));
  }
  const missing = byReason('not_in_snapshot');
  if (missing.length > 0) {
    lines.push(d.notInSnapshot(names(missing), missing.length > 1));
  }
  const excluded = byReason('excluded');
  if (excluded.length > 0) {
    lines.push(d.excludedTables(names(excluded), excluded.length > 1));
  }
  const columns = db.excluded_columns.filter((item) => item.reason === 'excluded').map((item) => `${item.table}.${item.column}`);
  if (columns.length > 0) {
    lines.push(d.excludedColumns(names(columns)));
  }
  if (sampled.length === 0 && byReason('samples_not_allowed').length > 0) {
    lines.push(d.samplesNotAllowed(db.connection_title));
  }
  return (
    <>
      {lines.map((line) => (
        <p key={line} className={styles.origin}>
          {line}
        </p>
      ))}
    </>
  );
}

/** Сообщение СУБД как есть: ту же строку сотрудник увидит в своей базе, поэтому оно не переводится. */
function DatabaseMessage({ label, text }: { label: string; text: string }) {
  return (
    <div className={styles.part}>
      <p className={styles.label}>{label}</p>
      <pre className={styles.message} lang="en">
        {text}
      </pre>
    </div>
  );
}

/** Что показал выполнившийся запрос: результат, число затронутых строк либо пояснение, почему показать нечего. */
function OutcomeView({ block, outcome, sample, emptyText }: { block: SqlRunBlock; outcome: Outcome; sample: boolean; emptyText: string | null }) {
  if (outcome === 'emptyRead' || outcome === 'emptyWrite') {
    return <p>{emptyText ?? (outcome === 'emptyRead' ? t.emptyReadText : t.emptyWriteText)}</p>;
  }
  return (
    <>
      {outcome === 'affected' && <p>{sample ? d.affected(block.affected_rows ?? 0) : t.affected(block.affected_rows ?? 0)}</p>}
      {block.result && (
        <div className={styles.part}>
          <p className={styles.label}>{t.result(rowCount(block.result))}</p>
          <RowsTable data={block.result} caption={t.resultCaption} />
        </div>
      )}
    </>
  );
}

function Headline({ kind, children }: { kind: 'ok' | 'warning' | 'error'; children: string }) {
  const Icon = { ok: IconCheckARegular16, warning: IconWarningTriangleRegular16, error: IconXCircleRegular16 }[kind];
  return (
    <p className={styles.headline}>
      <span className={`${styles.icon} ${styles[kind]}`} aria-hidden="true">
        <Icon />
      </span>
      {children}
    </p>
  );
}

/** Однострочное состояние без рамки, как «Синтаксис проверен»: значок и текст (§5.7). */
export function Line({ icon, children }: { icon?: React.ReactNode; children: string }) {
  return (
    // Не `<p>`: индикатор библиотеки — блочный элемент.
    <div className={styles.line}>
      {icon && (
        <span className={styles.icon} aria-hidden="true">
          {icon}
        </span>
      )}
      {children}
    </div>
  );
}

interface RecheckProps {
  /** «Проверить снова» у сохранённого блока проверки на образце (§5.7). */
  onRecheck?: () => void;
  /** Почему «Проверить снова» ждёт: идёт ответ или другая проверка (`Hint`). */
  recheckWait?: string | null;
}

/** Сохранённый итог проверки на образце: строк базы нет — только итог, оговорка и «Проверить снова» (§5.7). */
function Saved({ block, onRecheck, recheckWait }: { block: SqlRunBlock } & RecheckProps) {
  const outcome = outcomeOf(block);
  const empty = outcome === 'emptyRead' || outcome === 'emptyWrite';
  const sample = isSample(block);
  let headline: React.ReactNode;
  let detail: React.ReactNode = null;
  if (block.status === 'failed') {
    headline = <Headline kind="error">{sample ? d.failed : t.failed}</Headline>;
  } else if (block.status === 'fixed') {
    headline = <Headline kind="warning">{t.fixed}</Headline>;
  } else {
    headline = <Headline kind={empty ? 'warning' : 'ok'}>{empty ? t[outcome] : sample ? d.passed : t.passed}</Headline>;
  }
  if (block.error && block.status !== 'passed') {
    detail = (
      <>
        <DatabaseMessage label={block.status === 'fixed' ? t.originalError : t.dbMessage} text={block.error} />
        {block.error_kind === 'sqlstate' && <p className={styles.muted}>{d.sqlstateNote}</p>}
      </>
    );
  }
  const count = block.affected_rows !== null ? d.affectedCount(block.affected_rows) : d.rowsCount(rowCount(block.result));
  // Ждущая кнопка остаётся в обходе по Tab и объясняет причину (концепция §5.7, §9).
  const button = (
    <Button size="small" aria-disabled={recheckWait ? true : undefined} onClick={recheckWait ? undefined : onRecheck}>
      {d.recheck}
    </Button>
  );
  return (
    <div className={styles.frame}>
      {headline}
      {detail}
      {block.fixed_sql && <CodeBlock code={block.fixed_sql} language="sql" />}
      {block.status !== 'failed' && <p>{count}</p>}
      <p className={styles.muted}>{sample ? d.disclaimer : t.disclaimer}</p>
      <Origin block={block} />
      <p className={styles.muted}>{d.notStored}</p>
      {onRecheck && (
        <div>
          {recheckWait ? (
            <TitleHint text={recheckWait} always>
              {button}
            </TitleHint>
          ) : (
            button
          )}
        </div>
      )}
    </div>
  );
}

/** Итог с данными — в рамке; оговорка о придуманных данных стоит только здесь. */
function Framed({ block }: { block: SqlRunBlock }) {
  const outcome = outcomeOf(block);
  const empty = outcome === 'emptyRead' || outcome === 'emptyWrite';
  const sample = isSample(block);
  const mixed = sample && (block.fixture ?? []).some((table) => table.origin !== 'sample');
  const disclaimer = (
    <>
      <p className={styles.muted}>{sample ? `${d.disclaimer}${mixed ? `. ${d.disclaimerMixed}` : ''}` : t.disclaimer}</p>
      <Origin block={block} />
    </>
  );
  const fixture = block.fixture && <Fixture tables={block.fixture} sample={sample} />;
  const emptyText = sample ? (outcome === 'emptyRead' ? d.emptyReadText : d.emptyWriteText) : null;

  if (block.status === 'failed') {
    return (
      <div className={styles.frame}>
        <Headline kind="error">{sample ? d.failed : t.failed}</Headline>
        <p>{t.failedText}</p>
        {block.error && <DatabaseMessage label={t.dbMessage} text={block.error} />}
        {disclaimer}
        {fixture}
      </div>
    );
  }
  if (block.status === 'fixed') {
    return (
      <div className={styles.frame}>
        <Headline kind="warning">{t.fixed}</Headline>
        {block.error && <DatabaseMessage label={t.originalError} text={block.error} />}
        {block.fixed_sql && <CodeBlock code={block.fixed_sql} language="sql" />}
        {disclaimer}
        {fixture}
        <OutcomeView block={block} outcome={outcome} sample={sample} emptyText={emptyText} />
      </div>
    );
  }
  return (
    <div className={styles.frame}>
      <Headline kind={empty ? 'warning' : 'ok'}>{empty ? t[outcome] : sample ? d.passed : t.passed}</Headline>
      {empty && <OutcomeView block={block} outcome={outcome} sample={sample} emptyText={emptyText} />}
      {disclaimer}
      {fixture}
      {!empty && <OutcomeView block={block} outcome={outcome} sample={sample} emptyText={emptyText} />}
    </div>
  );
}

/**
 * Проверка блока `sql` на тестовых данных (концепция §5.7, контракт §7.4). Всё, что пришло из тестовой
 * базы, — недоверенные данные и выводится только текстом.
 */
export function SqlRunResult({ block, onRecheck, recheckWait }: { block: SqlRunBlock } & RecheckProps) {
  switch (block.status) {
    case 'pending':
      return <Line icon={<IconTimeClockRegular16 />}>{t.pending}</Line>;
    case 'running':
      return <Line icon={<Spinner type="mini" caption={null} />}>{block.db ? d.running : t.running}</Line>;
    case 'passed':
    case 'fixed':
    case 'failed':
      return isSaved(block) ? <Saved block={block} onRecheck={onRecheck} recheckWait={recheckWait} /> : <Framed block={block} />;
    case 'unavailable':
      return <Line icon={<IconInfoCircleRegular16 />}>{unavailableText(block)}</Line>;
    case 'timeout':
      return <Line icon={<IconTimeClockRegular16 />}>{t.timeout}</Line>;
    case 'stopped':
      return <Line>{t.stopped}</Line>;
    default:
      // «Не проверялось» с причиной; так же выглядит состояние, которого интерфейс не знает.
      return <Line>{notCheckedText(block)}</Line>;
  }
}
