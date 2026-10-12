import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Spinner } from '@skbkontur/react-ui';

import type { KbDocument, PortalConfig } from '../../api/types';
import { documentErrorText, hasReprocessFailure, reprocessFailureText } from '../../kb/documents';
import { formatDuration, texts } from '../../texts';
import styles from './StatusBadge.module.css';

const t = texts.kb;

/** Слово состояния и ход обработки: у записи — расшифрованные минуты, у документа — страницы. */
function processing(document: KbDocument): { label: string; detail: string | null } {
  const { progress } = document;
  if (document.kind === 'recording') {
    const seconds = progress && 'seconds_done' in progress ? progress : null;
    return {
      label: t.status.transcribing,
      detail: seconds && t.transcribed(formatDuration(seconds.seconds_done), formatDuration(seconds.seconds_total)),
    };
  }
  const pages = progress && 'pages_done' in progress ? progress : null;
  return {
    label: pages?.recognizing ? t.status.recognizing : t.status.processing,
    detail: pages && t.progress(pages.pages_done, pages.pages_total),
  };
}

interface StatusBadgeProps {
  document: KbDocument;
  /** Предел длины записи для текста причины; `null` — настройки не загрузились. */
  media: PortalConfig['media'] | null;
}

/**
 * Состояние документа (концепция §5.6): иконка и слово, ниже — ход, причина ошибки или уточнение к «Готов».
 * Что показано под «Готов», решают только поля документа.
 */
export function StatusBadge({ document, media }: StatusBadgeProps) {
  const { status } = document;
  let icon: React.ReactNode;
  let label: string;
  let detail: string | null = null;
  let warning: string | null = null;

  if (status === 'queued') {
    icon = <IconTimeClockRegular16 />;
    label = t.status.queued;
  } else if (status === 'processing') {
    icon = <Spinner type="mini" caption={null} />;
    ({ label, detail } = processing(document));
  } else if (status === 'ready') {
    icon = <IconCheckARegular16 />;
    label = t.status.ready;
    if (hasReprocessFailure(document)) {
      warning = reprocessFailureText(document.reprocess_error ?? '');
    } else if (document.reprocessing) {
      detail = t.reprocessing;
    }
  } else {
    icon = <IconXCircleRegular16 />;
    label = t.status.error;
    detail = documentErrorText(document, media);
  }

  return (
    <div className={styles.badge}>
      <span className={`${styles.line} ${styles[status]}`}>
        <span className={styles.icon} aria-hidden="true">
          {icon}
        </span>
        {label}
      </span>
      {detail && <span className={styles.detail}>{detail}</span>}
      {warning && (
        <span className={styles.warning}>
          <span className={styles.warningIcon} aria-hidden="true">
            <IconWarningTriangleRegular16 />
          </span>
          <span className={styles.detail}>{warning}</span>
        </span>
      )}
      {status === 'ready' && document.images_truncated && <span className={styles.detail}>{t.imagesTruncated}</span>}
    </div>
  );
}
