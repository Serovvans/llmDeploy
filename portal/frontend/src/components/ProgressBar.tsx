import styles from './ProgressBar.module.css';

interface ProgressBarProps {
  percent: number;
  /** Имя полосы для экранного чтения; без него полоса скрыта — рядом с ней всегда стоит число. */
  label?: string;
}

/** Полоса хода (концепция §7.3). Вслух ход не объявляется: он читается обычной навигацией (§9). */
export function ProgressBar({ percent, label }: ProgressBarProps) {
  const named = label
    ? { role: 'progressbar', 'aria-label': label, 'aria-valuenow': percent, 'aria-valuemin': 0, 'aria-valuemax': 100 }
    : { 'aria-hidden': true };
  return (
    <span className={styles.track} {...named}>
      <span className={styles.fill} style={{ width: `${percent}%` }} />
    </span>
  );
}
