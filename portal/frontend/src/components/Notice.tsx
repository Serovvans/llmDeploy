import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Link } from '@skbkontur/react-ui';

import styles from './Notice.module.css';

export type NoticeKind = 'info' | 'warning' | 'error';

interface NoticeAction {
  label: string;
  onClick: () => void;
}

interface NoticeProps {
  kind: NoticeKind;
  children: React.ReactNode;
  /** Одно действие или, с версии концепции 0.14, до двух — через « · » (§4.6). */
  action?: NoticeAction | [NoticeAction, NoticeAction];
}

const ICONS = {
  info: IconInfoCircleRegular16,
  warning: IconWarningTriangleRegular16,
  error: IconXCircleRegular16,
};

/** Сообщение по месту (концепция §4.6): вид различают иконка и подложка, текст всегда `--p-text`. */
export function Notice({ kind, children, action }: NoticeProps) {
  const Icon = ICONS[kind];
  const actions = action === undefined ? [] : Array.isArray(action) ? action : [action];
  return (
    <div className={`${styles.notice} ${styles[kind]}`} role={kind === 'error' ? 'alert' : 'status'}>
      <span className={styles.icon} aria-hidden="true">
        <Icon />
      </span>
      <span className={styles.text}>{children}</span>
      {actions.length > 0 && (
        <span className={styles.actions}>
          {actions.map((item, index) => (
            <span key={item.label}>
              {index > 0 && <span aria-hidden="true">{' · '}</span>}
              <Link component="button" onClick={item.onClick}>
                {item.label}
              </Link>
            </span>
          ))}
        </span>
      )}
    </div>
  );
}
