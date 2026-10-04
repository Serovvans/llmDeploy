import { Button, Center } from '@skbkontur/react-ui';

import styles from './EmptyState.module.css';

interface EmptyStateProps {
  title: string;
  text: string;
  action: { label: string; onClick: () => void };
  /** Служебный экран — заголовок страницы (`h1`); пустое состояние внутри экрана — `h2`. */
  headingLevel?: 'h1' | 'h2';
}

/** Пустое состояние и служебные экраны (концепция §4.8): заголовок, пояснение, одно действие. */
export function EmptyState({ title, text, action, headingLevel: Heading = 'h2' }: EmptyStateProps) {
  return (
    <Center className={styles.root}>
      <div className={styles.content}>
        <Heading className={styles.title}>{title}</Heading>
        <p className="p-muted">{text}</p>
        <div className={styles.action}>
          <Button onClick={action.onClick}>{action.label}</Button>
        </div>
      </div>
    </Center>
  );
}
