import styles from './StepList.module.css';

interface Step {
  title: string;
  children: React.ReactNode;
}

/** Нумерованные шаги (концепция §5.4): список `<ol>` с крупными цифрами. */
export function StepList({ steps }: { steps: Step[] }) {
  return (
    <ol className={styles.list}>
      {steps.map((step) => (
        <li key={step.title} className={styles.step}>
          <h2 className={styles.title}>{step.title}</h2>
          {step.children}
        </li>
      ))}
    </ol>
  );
}
