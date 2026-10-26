import { Tabs } from '@skbkontur/react-ui';
import { forwardRef } from 'react';
import { Link, useLocation } from 'react-router-dom';

import { PageHeader } from '../components/PageHeader';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import styles from './AdminHeader.module.css';

const a = texts.db.admin;

/** Вкладка — ссылка маршрутизатора (§5.14: вкладки ведут на `/admin/users` и `/admin/databases`). */
const TabLink = forwardRef<HTMLAnchorElement, React.AnchorHTMLAttributes<HTMLAnchorElement> & { href: string }>(
  function TabLink({ href, ...rest }, ref) {
    return <Link ref={ref} to={href} {...rest} />;
  },
);

/**
 * Заголовок экранов администратора (концепция §5.14, Д-59): при включённых подключениях — «Управление» и вкладки
 * «Пользователи» и «Базы данных», действие вкладки — справа на линии вкладок; иначе — как раньше.
 */
export function AdminHeader({ title, action }: { title: string; action: React.ReactNode }) {
  const { config } = useSession();
  const { pathname } = useLocation();
  if (!config?.db.enabled) {
    return <PageHeader title={title}>{action}</PageHeader>;
  }
  return (
    <>
      <PageHeader title={a.section} />
      <div className={styles.tabs}>
        <Tabs<string> value={pathname}>
          <Tabs.Tab id="/admin/users" href="/admin/users" component={TabLink}>
            {a.tabUsers}
          </Tabs.Tab>
          <Tabs.Tab id="/admin/databases" href="/admin/databases" component={TabLink}>
            {a.tabDatabases}
          </Tabs.Tab>
        </Tabs>
        {action}
      </div>
    </>
  );
}
