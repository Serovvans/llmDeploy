import styles from './PageHeader.module.css';

/** Строка заголовка рабочей области: название раздела слева, действия справа (концепция §3.2). */
export function PageHeader({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <header className={styles.header}>
      <h1 className={styles.title}>{title}</h1>
      {children}
    </header>
  );
}
