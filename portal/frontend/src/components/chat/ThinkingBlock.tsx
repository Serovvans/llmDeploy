import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { Spinner } from '@skbkontur/react-ui';
import { useEffect, useId, useState } from 'react';

import { texts } from '../../texts';
import styles from './ThinkingBlock.module.css';

interface ThinkingBlockProps {
  text: string;
  /** Секунды размышлений; `null` — неизвестно. */
  seconds: number | null;
  /** Модель рассуждает прямо сейчас: момент начала, секунды считает интерфейс. */
  startedAt: number | null;
}

function useElapsedSeconds(startedAt: number | null): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (startedAt === null) {
      return;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [startedAt]);
  return startedAt === null ? 0 : Math.max(0, Math.round((now - startedAt) / 1000));
}

/** Размышления модели: строка-кнопка, свёрнута по умолчанию (концепция §4.2). */
export function ThinkingBlock({ text, seconds, startedAt }: ThinkingBlockProps) {
  const [open, setOpen] = useState(false);
  const elapsed = useElapsedSeconds(startedAt);
  const contentId = useId();
  const Arrow = open ? IconArrowCDownRegular16 : IconArrowCRightRegular16;

  let label: string = texts.chat.thinking;
  if (startedAt !== null) {
    label = texts.chat.thinkingNow(elapsed);
  } else if (seconds !== null) {
    label = texts.chat.thinkingFor(seconds);
  }

  return (
    <div>
      <button
        type="button"
        className={styles.toggle}
        aria-expanded={open}
        aria-controls={contentId}
        onClick={() => setOpen((value) => !value)}
      >
        {startedAt === null ? <Arrow aria-hidden="true" /> : <Spinner type="mini" caption={null} />}
        <span>{label}</span>
      </button>
      {open && (
        <p id={contentId} className={styles.text}>
          {text}
        </p>
      )}
    </div>
  );
}
