import styles from './ProgressBar.module.css';

interface ProgressBarProps {
  percent: number;
  /** Имя полосы для экранного чтения; без него полоса скрыта — рядом с ней всегда стоит число. */
  label?: string;
  /** Заполнение цветом предупреждения — у памяти диалога «почти заполнена» и «переполнена» (концепция §7.3). */
  warning?: boolean;
  /** Размер полосы: компактный индикатор памяти и полоса в панели памяти. */
  className?: string;
}

/** Полоса хода (концепция §7.3). Вслух ход не объявляется: он читается обычной навигацией (§9). */
export function ProgressBar({ percent, label, warning = false, className }: ProgressBarProps) {
  const named = label
    ? { role: 'progressbar', 'aria-label': label, 'aria-valuenow': percent, 'aria-valuemin': 0, 'aria-valuemax': 100 }
    : { 'aria-hidden': true };
  return (
    <span className={className ? `${styles.track} ${className}` : styles.track} {...named}>
      <span className={warning ? `${styles.fill} ${styles.warning}` : styles.fill} style={{ width: `${percent}%` }} />
    </span>
  );
}
