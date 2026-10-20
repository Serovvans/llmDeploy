import { useLayoutEffect, useRef } from 'react';

import styles from './MemoryLayout.module.css';

interface MemoryLayoutProps {
  /** Панель памяти или `null`, когда она скрыта или её нет на экране. */
  panel: React.ReactNode;
  /** Заметка о длинном диалоге и панель запроса: поверх них панель памяти не встаёт (концепция §3.3, Д-32). */
  bottom: React.ReactNode;
  /** Строка действий, заметки над лентой и сама лента. */
  children: React.ReactNode;
}

/**
 * Рабочая область под строкой заголовка: слева лента с панелью запроса, справа панель памяти.
 * В окне уже 1440 px панель встаёт поверх ленты до верхнего края нижнего блока — его высота
 * передаётся стилям переменной: панель запроса меняет высоту по мере набора текста.
 */
export function MemoryLayout({ panel, bottom, children }: MemoryLayoutProps) {
  const main = useRef<HTMLDivElement>(null);
  const lower = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const root = main.current;
    const element = lower.current;
    if (!root || !element || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => root.style.setProperty('--p-memory-bottom', `${element.offsetHeight}px`));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  return (
    <div ref={main} className={styles.main}>
      <div className={styles.dialog}>
        <div className={styles.upper}>{children}</div>
        <div ref={lower} className={styles.lower}>
          {bottom}
        </div>
      </div>
      {panel}
    </div>
  );
}
