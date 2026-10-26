import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Link, Spinner } from '@skbkontur/react-ui';

import type { AdminDbConnection, RightsFinding } from '../api/types';
import { useCopy } from '../hooks/useCopy';
import { formatDate, texts } from '../texts';
import { clipboardText, findingLine, reasonText } from './rights';
import styles from './RightsCheck.module.css';
import { snapshotDay } from './selection';

const r = texts.db.rights;

function Findings({ items }: { items: RightsFinding[] }) {
  return (
    <ul className={styles.findings}>
      {items.map((finding) => {
        const { left, right } = findingLine(finding);
        return (
          <li key={`${finding.kind}-${right}`} className={styles.finding}>
            <span>{left}</span>
            <span className={styles.exact}>{right}</span>
          </li>
        );
      })}
    </ul>
  );
}

function Warnings({ items }: { items: RightsFinding[] }) {
  return (
    <div className={styles.block}>
      <p className={styles.warningTitle}>
        <span className={styles.warningIcon} aria-hidden="true">
          <IconWarningTriangleRegular16 />
        </span>
        {r.warning}
      </p>
      <Findings items={items} />
      <p className={styles.muted}>{r.definerAdvice}</p>
    </div>
  );
}

function Headline({ kind, children }: { kind: 'ok' | 'error'; children: string }) {
  return (
    <p className={styles.headline}>
      <span className={kind === 'ok' ? styles.okIcon : styles.errorIcon} aria-hidden="true">
        {kind === 'ok' ? <IconCheckARegular16 /> : <IconXCircleRegular16 />}
      </span>
      {children}
    </p>
  );
}

interface Props {
  connection: AdminDbConnection;
  checking: boolean;
  /** «Загрузить схему» у работающего подключения без схемы. */
  onLoadSnapshot: () => void;
  loadingSnapshot: boolean;
}

/** Итог проверки подключения (свой: `RightsCheck`, концепция §5.14, ПД §8.6). */
export function RightsCheck({ connection, checking, onLoadSnapshot, loadingSnapshot }: Props) {
  const copy = useCopy();
  if (checking) {
    return <Spinner type="mini" caption={r.checking} />;
  }
  const { rights, last_error: lastError, snapshot } = connection;
  const lastFailed = lastError && rights.status !== 'unknown' && (
    <p className={styles.lastFailed}>
      <span className={styles.warningIcon} aria-hidden="true">
        <IconWarningTriangleRegular16 />
      </span>
      {r.lastFailed(reasonText(connection), formatDate(lastError.at))}
    </p>
  );
  const copyLink = (
    <div>
      <Link component="button" onClick={() => void copy.copy(clipboardText(connection))}>
        {copy.label === texts.common.copied ? copy.label : r.copy}
      </Link>
    </div>
  );

  if (rights.status === 'unknown') {
    if (!lastError) {
      return <p className={styles.muted}>{r.notChecked}</p>;
    }
    return (
      <div className={styles.check}>
        <Headline kind="error">{r.failed}</Headline>
        <p>{reasonText(connection)}</p>
        <p className={styles.muted}>{formatDate(lastError.at)}</p>
      </div>
    );
  }

  if (rights.status === 'read_only') {
    return (
      <div className={styles.check}>
        <Headline kind="ok">{r.readOnly}</Headline>
        {rights.checked_at && <p className={styles.muted}>{r.readOnlyNote(formatDate(rights.checked_at))}</p>}
        {!snapshot && (
          <div>
            <Button size="small" loading={loadingSnapshot} onClick={onLoadSnapshot}>
              {r.loadSnapshot}
            </Button>
          </div>
        )}
        {rights.warnings.length > 0 && (
          <>
            <Warnings items={rights.warnings} />
            {copyLink}
          </>
        )}
        {lastFailed}
      </div>
    );
  }

  return (
    <div className={styles.check}>
      <Headline kind="error">{r.excess(connection.username)}</Headline>
      <p>{r.excessText(snapshot ? snapshotDay(snapshot.taken_at) : null)}</p>
      <Findings items={rights.findings} />
      {copyLink}
      <p className={styles.muted}>{r.memo}</p>
      {rights.warnings.length > 0 && <Warnings items={rights.warnings} />}
      {lastFailed}
    </div>
  );
}
