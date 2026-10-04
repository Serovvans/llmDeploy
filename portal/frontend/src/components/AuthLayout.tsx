import { Center } from '@skbkontur/react-ui';

import styles from './AuthLayout.module.css';

interface AuthLayoutProps {
  title: string;
  /** Колонка 360 px; экран настройки второго фактора шире — 720 px (концепция §5.4). */
  wide?: boolean;
  children: React.ReactNode;
}

/** Каркас экранов входа: колонка по центру, один заголовок `h1` (концепция §5.1). */
export function AuthLayout({ title, wide = false, children }: AuthLayoutProps) {
  return (
    <Center className={styles.root}>
      <main className={wide ? styles.wide : styles.column}>
        <h1 className={styles.title}>{title}</h1>
        {children}
      </main>
    </Center>
  );
}
