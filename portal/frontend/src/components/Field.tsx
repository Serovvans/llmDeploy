import { useId } from 'react';

import styles from './Field.module.css';

interface FieldProps {
  label: string;
  error?: string | null;
  hint?: React.ReactNode;
  /** Подпись скрыта визуально, но остаётся доступным именем поля. */
  hideLabel?: boolean;
  children: (control: { id: string; 'aria-describedby': string | undefined; error: boolean }) => React.ReactNode;
}

/** Поле формы: подпись, поле, подсказка и текст ошибки, связанный с полем (концепция §4.5, §9). */
export function Field({ label, error, hint, hideLabel = false, children }: FieldProps) {
  const id = useId();
  const errorId = `${id}-error`;
  const hintId = `${id}-hint`;
  const describedBy = [error ? errorId : null, hint ? hintId : null].filter(Boolean).join(' ') || undefined;

  return (
    <div className={styles.field}>
      <label htmlFor={id} className={hideLabel ? 'p-visually-hidden' : styles.label}>
        {label}
      </label>
      {children({ id, 'aria-describedby': describedBy, error: Boolean(error) })}
      {error && (
        <p id={errorId} className={styles.error} role="alert">
          {error}
        </p>
      )}
      {hint && (
        <p id={hintId} className={styles.hint}>
          {hint}
        </p>
      )}
    </div>
  );
}
