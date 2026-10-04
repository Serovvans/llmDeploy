import { useEffect } from 'react';

import { texts } from '../texts';

/** Заголовок вкладки: «Чат — Портал сотрудников» (концепция §3.2). */
export function usePageTitle(title: string): void {
  useEffect(() => {
    document.title = title === texts.product ? title : `${title} — ${texts.product}`;
  }, [title]);
}
