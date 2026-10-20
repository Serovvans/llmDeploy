import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { Hint, Link, Spinner } from '@skbkontur/react-ui';
import { useEffect, useId, useState } from 'react';

import type { DialogMemory } from '../../memory/useDialogMemory';
import { texts } from '../../texts';
import { ProgressBar } from '../ProgressBar';
import { isWarningLevel, levelHint, levelPercent } from './levelHint';
import styles from './MemoryMeter.module.css';

const t = texts.memory;
const SPINNER_DELAY_MS = 300;

/** Индикатор загрузки первой оценки — через 300 мс: быстрый ответ не мигает (концепция §4.9). */
function useDelayed(on: boolean): boolean {
  const [shown, setShown] = useState(false);
  useEffect(() => {
    if (!on) {
      return;
    }
    const timer = window.setTimeout(() => setShown(true), SPINNER_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      setShown(false);
    };
  }, [on]);
  return on && shown;
}

interface MemoryMeterProps {
  memory: DialogMemory;
  panelId: string;
  panelOpen: boolean;
  onOpen: () => void;
}

/**
 * Индикатор памяти диалога под панелью запроса (свой: `MemoryMeter`, концепция §4.9): уровень словом,
 * компактная полоса, процент — только в подсказке. Нажатие открывает панель памяти.
 */
export function MemoryMeter({ memory, panelId, panelOpen, onOpen }: MemoryMeterProps) {
  const hintId = useId();
  const loading = memory.level === null && memory.status === 'loading';
  const spinner = useDelayed(loading);
  const hint = levelHint(memory);

  let content: React.ReactNode;
  if (loading) {
    content = (
      <>
        <span className={styles.caption}>{t.title}</span>
        {spinner && <Spinner type="mini" caption={null} />}
      </>
    );
  } else if (memory.status === 'failed' || memory.level === null) {
    content = (
      <>
        <span className={styles.caption}>{t.meterCaption}</span> <span className={styles.level}>{t.unavailable}</span>
      </>
    );
  } else {
    content = (
      <>
        {memory.level === 'overflow' && (
          <span className={styles.warning} aria-hidden="true">
            <IconWarningTriangleRegular16 />
          </span>
        )}
        <span className={styles.caption}>{t.meterCaption}</span> <span className={styles.level}>{t.levels[memory.level]}</span>
        <ProgressBar percent={levelPercent(memory)} warning={isWarningLevel(memory)} className={styles.bar} />
      </>
    );
  }

  const button = (
    <Link
      component="button"
      className={styles.meter}
      aria-expanded={panelOpen}
      aria-controls={panelId}
      aria-describedby={hint ? hintId : undefined}
      onClick={onOpen}
    >
      <span className={styles.content}>{content}</span>
    </Link>
  );
  return (
    <span className={styles.root}>
      {hint ? (
        <Hint text={hint} pos="top right" maxWidth={320}>
          {button}
        </Hint>
      ) : (
        button
      )}
      {hint && (
        <span id={hintId} className="p-visually-hidden">
          {hint}
        </span>
      )}
    </span>
  );
}
