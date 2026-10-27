import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Link } from '@skbkontur/react-ui';
import { useEffect, useId, useState } from 'react';

import { api } from '../api/client';
import type { CodeConfig, CodeEdit, CodeEditFull, CodeKind, CodeVerification } from '../api/types';
import { texts } from '../texts';
import styles from './EditProposal.module.css';
import { formatDateTime, lineDiffStats, rejectedReason } from './rules';

const e = texts.workshop.edit;
/** Строк «Не принято порталом» и «Что осталось» — до пяти (§5.13.11). */
const LINES_SHOWN = 5;

/** Предложенные тексты правок: читаются один раз — они не меняются (`after` — из ответа модели). */
const proposals = new Map<string, Promise<CodeEditFull>>();

export function loadEdit(projectId: string, messageId: string, fresh = false): Promise<CodeEditFull> {
  const known = proposals.get(messageId);
  if (known && !fresh) {
    return known;
  }
  const request = api.getCodeEdit(projectId, messageId);
  proposals.set(messageId, request);
  request.catch(() => proposals.delete(messageId));
  return request;
}

function Verification({ verification }: { verification: CodeVerification }) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const errors = verification.diagnostics.filter((item) => item.severity === 'error');
  let line: React.ReactNode;
  switch (verification.status) {
    case 'succeeded':
      if (verification.findings.length > 0) {
        line = (
          <span className={styles.line}>
            <IconXCircleRegular16 className={styles.error} />
            {e.findings(verification.findings.length)}
          </span>
        );
      } else if (verification.warnings > 0) {
        line = (
          <span className={styles.line}>
            <IconCheckARegular16 />
            {e.builds} <span className={styles.muted}>{e.warnings(verification.warnings)}</span>
          </span>
        );
      } else {
        line = (
          <span className={styles.line}>
            <IconCheckARegular16 />
            {e.verified}
          </span>
        );
      }
      break;
    case 'failed':
      line = (
        <>
          <span className={styles.line}>
            <IconXCircleRegular16 className={styles.error} />
            {e.failed(errors.length)}{' '}
            <Link component="button" aria-expanded={open} aria-controls={listId} onClick={() => setOpen((value) => !value)}>
              {e.remaining}
            </Link>
          </span>
          {open && (
            <ul id={listId} className={styles.remaining}>
              {errors.slice(0, LINES_SHOWN).map((item, index) => (
                <li key={index}>{e.remainingLine(item.file, item.line, item.message)}</li>
              ))}
            </ul>
          )}
        </>
      );
      break;
    default:
      line = (
        <span className={styles.line}>
          <IconInfoCircleRegular16 className={styles.muted} />
          {e.notVerified[verification.status]}
        </span>
      );
  }
  return (
    <div className={styles.verification}>
      {line}
      <span className={styles.muted}>{e.builtTimes(verification.builds)}</span>
    </div>
  );
}

function reasonSuffix(reason: string, config: CodeConfig, kind: CodeKind): string {
  const text = rejectedReason(reason, config, kind);
  return text ? ` — ${text}` : '';
}

interface Props {
  projectId: string;
  messageId: string;
  edit: CodeEdit;
  config: CodeConfig;
  /** Вид проекта (1.0): тексты отклонённых файлов веб-видов. */
  kind?: CodeKind;
  /** Текущий текст файла проекта по пути, с несохранёнными правками; `null` — файла нет. */
  currentText: (path: string) => string | null;
  /** Решение сейчас недоступно: идёт решение по этой правке. */
  pending: boolean;
  onReview: () => void;
  onReject: () => void;
  /** `opener` — значение `data-opener` у обёртки ссылки «Версии»: на неё вернётся фокус. */
  onVersions: (opener: string) => void;
}

/** Карточка правки под ответом помощника (концепция §5.13.11). */
export function EditProposal({ projectId, messageId, edit, config, kind = 'gptool', currentText, pending, onReview, onReject, onVersions }: Props) {
  const [after, setAfter] = useState<Map<string, string> | null>(null);
  const proposed = edit.status === 'proposed';

  // «+N −M» — по сравнению с текущим файлом: нужен предложенный текст, он есть только в полной правке.
  useEffect(() => {
    if (!proposed || edit.files.length === 0) {
      return;
    }
    let cancelled = false;
    loadEdit(projectId, messageId).then(
      (full) => {
        if (!cancelled) {
          setAfter(new Map(full.files.map((file) => [file.path, file.after])));
        }
      },
      () => {
        // Без чисел карточка остаётся рабочей.
      },
    );
    return () => {
      cancelled = true;
    };
  }, [edit.files.length, messageId, projectId, proposed]);

  const rejected = edit.rejected_files;
  const rejectedLines = (
    <>
      {rejected.slice(0, LINES_SHOWN).map((item, index) => (
        <p key={index} className={`${styles.line} ${styles.muted}`}>
          <IconWarningTriangleRegular16 className={styles.warning} />
          <span>
            {e.notAccepted}
            <span className={styles.mono}>{item.path}</span>
            {reasonSuffix(item.reason, config, kind)}
          </span>
        </p>
      ))}
      {rejected.length > LINES_SHOWN && <p className={styles.muted}>{e.andMore(rejected.length - LINES_SHOWN)}</p>}
    </>
  );

  if (edit.files.length === 0) {
    return (
      <div className={styles.card}>
        <p>{e.nothing}</p>
        {rejectedLines}
      </div>
    );
  }

  const accepted = edit.files.filter((file) => file.decision === 'accepted').length;
  let decision: React.ReactNode;
  if (edit.status === 'proposed') {
    decision = (
      <div className={styles.actions}>
        <Button size="small" use="primary" disabled={pending} onClick={onReview}>
          {e.review}
        </Button>
        <Link component="button" disabled={pending} onClick={onReject}>
          {e.reject}
        </Link>
      </div>
    );
  } else if (edit.status === 'accepted') {
    const date = edit.decided_at ? formatDateTime(edit.decided_at) : '';
    decision = (
      <p className={styles.line}>
        <IconCheckARegular16 />
        {accepted === edit.files.length ? e.accepted(date) : e.acceptedPart(accepted, edit.files.length, date)}{' '}
        <span data-opener={`edit-versions-${messageId}`}>
          <Link component="button" onClick={() => onVersions(`edit-versions-${messageId}`)}>{e.versions}</Link>
        </span>
      </p>
    );
  } else {
    decision = <p className={styles.muted}>{edit.status === 'rejected' ? e.rejected : e.superseded}</p>;
  }

  // Без вкладок фокус после решения возвращается на карточку (§9): её находит экран проекта.
  return (
    <div className={styles.card} tabIndex={-1} data-edit-card={messageId}>
      <h3 className={styles.title}>{e.title(edit.files.length)}</h3>
      <ul className={styles.files}>
        {edit.files.map((file) => {
          const proposal = after?.get(file.path);
          const stats = proposed && proposal !== undefined ? lineDiffStats(currentText(file.path) ?? '', proposal) : null;
          return (
            <li key={file.path} className={styles.file}>
              <span className={styles.path}>{file.path}</span>
              <span className={styles.muted}>{file.change === 'added' ? e.added : e.modified}</span>
              {stats && (
                <span className={styles.stats}>
                  {stats.added > 0 && <span>+{stats.added}</span>}
                  {stats.removed > 0 && <span> −{stats.removed}</span>}
                </span>
              )}
            </li>
          );
        })}
      </ul>
      {rejectedLines}
      {edit.verification && <Verification verification={edit.verification} />}
      {decision}
    </div>
  );
}
