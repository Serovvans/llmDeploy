import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Spinner } from '@skbkontur/react-ui';

import type { KbDocument } from '../../api/types';
import { documentErrorText } from '../../kb/documents';
import { texts } from '../../texts';
import styles from './StatusBadge.module.css';

const t = texts.kb;

/** Состояние документа (концепция §5.6): иконка и слово, второй строкой — ход или причина ошибки. */
export function StatusBadge({ document }: { document: KbDocument }) {
  const { status, progress } = document;
  let icon: React.ReactNode;
  let label: string;
  let detail: string | null = null;

  if (status === 'queued') {
    icon = <IconTimeClockRegular16 />;
    label = t.status.queued;
  } else if (status === 'processing') {
    icon = <Spinner type="mini" caption={null} />;
    label = progress?.recognizing ? t.status.recognizing : t.status.processing;
    detail = progress ? t.progress(progress.pages_done, progress.pages_total) : null;
  } else if (status === 'ready') {
    icon = <IconCheckARegular16 />;
    label = t.status.ready;
  } else {
    icon = <IconXCircleRegular16 />;
    label = t.status.error;
    detail = documentErrorText(document);
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
    </div>
  );
}
