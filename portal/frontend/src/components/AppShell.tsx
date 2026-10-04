import type { SessionUser } from '../api/types';
import { texts } from '../texts';
import { AppRail } from './AppRail';
import styles from './AppShell.module.css';

const CONTENT_ID = 'content';

/** Каркас рабочих экранов (концепция §3.2, §3.3): рейка и рабочая область, страница целиком не прокручивается. */
export function AppShell({ user, children }: { user: SessionUser; children: React.ReactNode }) {
  return (
    <div className={styles.shell}>
      <a className={styles.skip} href={`#${CONTENT_ID}`}>
        {texts.common.skipToContent}
      </a>
      <AppRail user={user} />
      <main id={CONTENT_ID} className={styles.main} tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}
