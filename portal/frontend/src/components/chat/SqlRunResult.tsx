import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Spinner } from '@skbkontur/react-ui';
import { useId, useState } from 'react';

import type { SqlRun, SqlRunBlock, SqlRunRows } from '../../api/types';
import { texts } from '../../texts';
import { CodeBlock } from './CodeBlock';
import styles from './SqlRunResult.module.css';

const t = texts.sql.run;

/** Чем закончился выполнившийся запрос: показал строки или изменения либо данные не покрыли его условия. */
type Outcome = 'rows' | 'affected' | 'emptyRead' | 'emptyWrite';

function outcomeOf(block: SqlRunBlock): Outcome {
  if (block.affected_rows !== null) {
    return block.affected_rows > 0 ? 'affected' : 'emptyWrite';
  }
  return block.result && block.result.rows.length > 0 ? 'rows' : 'emptyRead';
}

function notCheckedText(block: SqlRunBlock): string {
  return (block.status === 'not_checked' && t.reasons[block.reason ?? '']) || t.notChecked;
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
        return t.announce.rows(block.result?.rows.length ?? 0);
      }
      return outcome === 'affected' ? t.announce.affected(block.affected_rows ?? 0) : t.announce[outcome];
    }
    case 'fixed':
      return t.announce.fixed;
    case 'failed':
      return t.announce.failed;
    case 'unavailable':
      return t.unavailable;
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
            {data.rows.map((row, rowIndex) => (
              <tr key={rowIndex}>
                {row.map((cell, index) => (
                  <td key={index}>{cell ?? <span className={styles.null}>{t.null}</span>}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {data.truncated && <p className={styles.muted}>{t.truncated(data.rows.length)}</p>}
    </>
  );
}

/** «Тестовые данные · 2 таблицы»: свёрнуты по умолчанию — сначала нужен итог (концепция §5.7). */
function Fixture({ tables }: { tables: NonNullable<SqlRunBlock['fixture']> }) {
  const [open, setOpen] = useState(false);
  const contentId = useId();
  const Arrow = open ? IconArrowCDownRegular16 : IconArrowCRightRegular16;
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
        <span>{t.fixture(tables.length)}</span>
      </button>
      {open && (
        <div id={contentId} className={styles.fixture}>
          {tables.map((table, index) => (
            <div key={index} className={styles.part}>
              <p className={`p-mono ${styles.tableName}`}>{table.table}</p>
              <RowsTable data={table} caption={t.fixtureCaption(table.table)} />
            </div>
          ))}
        </div>
      )}
    </div>
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
function OutcomeView({ block, outcome }: { block: SqlRunBlock; outcome: Outcome }) {
  if (outcome === 'emptyRead' || outcome === 'emptyWrite') {
    return <p>{outcome === 'emptyRead' ? t.emptyReadText : t.emptyWriteText}</p>;
  }
  return (
    <>
      {outcome === 'affected' && <p>{t.affected(block.affected_rows ?? 0)}</p>}
      {block.result && (
        <div className={styles.part}>
          <p className={styles.label}>{t.result(block.result.rows.length)}</p>
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

/** Состояние без данных — одна строка без рамки, как «Синтаксис проверен». */
function Line({ icon, children }: { icon?: React.ReactNode; children: string }) {
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

/** Итог с данными — в рамке; оговорка о придуманных данных стоит только здесь. */
function Framed({ block }: { block: SqlRunBlock }) {
  const outcome = outcomeOf(block);
  const empty = outcome === 'emptyRead' || outcome === 'emptyWrite';
  const disclaimer = <p className={styles.muted}>{t.disclaimer}</p>;
  const fixture = block.fixture && <Fixture tables={block.fixture} />;

  if (block.status === 'failed') {
    return (
      <div className={styles.frame}>
        <Headline kind="error">{t.failed}</Headline>
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
        <OutcomeView block={block} outcome={outcome} />
      </div>
    );
  }
  return (
    <div className={styles.frame}>
      <Headline kind={empty ? 'warning' : 'ok'}>{empty ? t[outcome] : t.passed}</Headline>
      {empty && <OutcomeView block={block} outcome={outcome} />}
      {disclaimer}
      {fixture}
      {!empty && <OutcomeView block={block} outcome={outcome} />}
    </div>
  );
}

/**
 * Проверка блока `sql` на тестовых данных (концепция §5.7, контракт §7.4). Всё, что пришло из тестовой
 * базы, — недоверенные данные и выводится только текстом.
 */
export function SqlRunResult({ block }: { block: SqlRunBlock }) {
  switch (block.status) {
    case 'pending':
      return <Line icon={<IconTimeClockRegular16 />}>{t.pending}</Line>;
    case 'running':
      return <Line icon={<Spinner type="mini" caption={null} />}>{t.running}</Line>;
    case 'passed':
    case 'fixed':
    case 'failed':
      return <Framed block={block} />;
    case 'unavailable':
      return <Line icon={<IconInfoCircleRegular16 />}>{t.unavailable}</Line>;
    case 'timeout':
      return <Line icon={<IconTimeClockRegular16 />}>{t.timeout}</Line>;
    case 'stopped':
      return <Line>{t.stopped}</Line>;
    default:
      // «Не проверялось» с причиной; так же выглядит состояние, которого интерфейс не знает.
      return <Line>{notCheckedText(block)}</Line>;
  }
}
