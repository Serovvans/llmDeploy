import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Link } from '@skbkontur/react-ui';

import styles from './Notice.module.css';

export type NoticeKind = 'info' | 'warning' | 'error';

interface NoticeProps {
  kind: NoticeKind;
  children: React.ReactNode;
  action?: { label: string; onClick: () => void };
}

const ICONS = {
  info: IconInfoCircleRegular16,
  warning: IconWarningTriangleRegular16,
  error: IconXCircleRegular16,
};

/** Сообщение по месту (концепция §4.6): вид различают иконка и подложка, текст всегда `--p-text`. */
export function Notice({ kind, children, action }: NoticeProps) {
  const Icon = ICONS[kind];
  return (
    <div className={`${styles.notice} ${styles[kind]}`} role={kind === 'error' ? 'alert' : 'status'}>
      <span className={styles.icon} aria-hidden="true">
        <Icon />
      </span>
      <span className={styles.text}>{children}</span>
      {action && (
        <Link component="button" onClick={action.onClick}>
          {action.label}
        </Link>
      )}
    </div>
  );
}
