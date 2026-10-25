import type { SessionUser } from '../api/types';
import { texts } from '../texts';
import { AppRail } from './AppRail';
import styles from './AppShell.module.css';

const CONTENT_ID = 'content';

/**
 * A screen may name its main working element with `data-skip-target` (the editor in «Код», §9); the skip link
 * focuses it, otherwise — or while it is hidden — the link leads to `<main>` as usual.
 */
function toWorkingElement(event: React.MouseEvent) {
  const target = document.querySelector<HTMLElement>('[data-skip-target]');
  if (!target) {
    return;
  }
  event.preventDefault();
  target.focus();
  if (!target.contains(document.activeElement)) {
    document.getElementById(CONTENT_ID)?.focus();
  }
}

/** Каркас рабочих экранов (концепция §3.2, §3.3): рейка и рабочая область, страница целиком не прокручивается. */
export function AppShell({ user, children }: { user: SessionUser; children: React.ReactNode }) {
  return (
    <div className={styles.shell}>
      <a className={styles.skip} href={`#${CONTENT_ID}`} onClick={toWorkingElement}>
        {texts.common.skipToContent}
      </a>
      <AppRail user={user} />
      <main id={CONTENT_ID} className={styles.main} tabIndex={-1}>
        {children}
      </main>
    </div>
  );
}
