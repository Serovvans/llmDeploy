import styles from './StepList.module.css';

interface Step {
  /** Без заголовка шаг состоит только из своего содержимого (окно архива, концепция §5.13.9, шаг 3). */
  title?: string;
  children: React.ReactNode;
}

/** Нумерованные шаги (концепция §5.4): список `<ol>` с крупными цифрами. */
export function StepList({ steps }: { steps: Step[] }) {
  return (
    <ol className={styles.list}>
      {steps.map((step, index) => (
        <li key={index} className={styles.step}>
          {step.title && <h2 className={styles.title}>{step.title}</h2>}
          {step.children}
        </li>
      ))}
    </ol>
  );
}
