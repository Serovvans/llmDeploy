import { useLayoutEffect, useRef, useState } from 'react';

import styles from './PageHeader.module.css';
import { TitleHint } from './TitleHint';

/** Наименьшая ширина названия рядом с настройками (концепция §5.7); то же значение — в стилях. */
const TITLE_MIN_PX = 120;

/**
 * Не помещаются ли настройки в одну строку с названием и действиями. Тогда группа настроек целиком
 * уходит на вторую строку (концепция §5.7): ничего не скрывается и не обрезается.
 */
function useSettingsWrap(
  headerRef: React.RefObject<HTMLElement | null>,
  groupRef: React.RefObject<HTMLElement | null>,
): boolean {
  const [wrapped, setWrapped] = useState(false);

  useLayoutEffect(() => {
    const row = headerRef.current;
    const group = groupRef.current;
    if (!row || !group || typeof ResizeObserver === 'undefined') {
      return;
    }
    const measure = () => {
      const style = getComputedStyle(row);
      const gap = parseFloat(style.columnGap) || 0;
      const inner = row.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight);
      // Сколько места нужно всему, кроме названия. Ширина группы настроек берётся по её содержимому:
      // сама группа на второй строке растянута на всю ширину.
      const others = Array.from(row.children)
        .slice(1)
        .reduce((sum, child) => sum + (child.contains(group) ? group : (child as HTMLElement)).offsetWidth + gap, 0);
      setWrapped(TITLE_MIN_PX + others > inner);
    };
    const observer = new ResizeObserver(measure);
    observer.observe(row);
    observer.observe(group);
    // Состав строки меняется без изменения её размеров: у созданного диалога появляется `Kebab`.
    const composition = new MutationObserver(measure);
    composition.observe(row, { childList: true });
    return () => {
      observer.disconnect();
      composition.disconnect();
    };
  }, [headerRef, groupRef]);

  return wrapped;
}

interface PageHeaderProps {
  title: string;
  /** Поле правки названия на месте названия (концепция §5.5); настройки на это время — на второй строке. */
  editor?: React.ReactNode;
  /** Настройки справа от названия; при нехватке ширины переносятся на вторую строку целиком. */
  settings?: React.ReactNode;
  children?: React.ReactNode;
}

/** Строка заголовка рабочей области: название раздела слева, действия справа (концепция §3.2). */
export function PageHeader({ title, editor, settings, children }: PageHeaderProps) {
  const headerRef = useRef<HTMLElement>(null);
  const groupRef = useRef<HTMLDivElement>(null);
  // Полю правки с текстом ошибки справа нужна вся строка.
  const wrapped = useSettingsWrap(headerRef, groupRef) || editor !== undefined;
  if (settings === undefined) {
    return (
      <header className={styles.header}>
        {editor ?? <h1 className={styles.title}>{title}</h1>}
        {children}
      </header>
    );
  }
  return (
    <header ref={headerRef} className={wrapped ? `${styles.header} ${styles.wrapped}` : styles.header}>
      {editor ?? (
        <TitleHint text={title} pos="bottom left" className={styles.titleBox}>
          <h1 className={styles.title}>{title}</h1>
        </TitleHint>
      )}
      <div className={styles.settings}>
        <div ref={groupRef} className={styles.settingsGroup}>
          {settings}
        </div>
      </div>
      {children}
    </header>
  );
}
