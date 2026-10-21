import { Hint } from '@skbkontur/react-ui';
import { useState } from 'react';

interface TitleHintProps {
  /** Полный текст названия. */
  text: string;
  pos?: React.ComponentProps<typeof Hint>['pos'];
  maxWidth?: React.ComponentProps<typeof Hint>['maxWidth'];
  /** Показывать и у необрезанного текста: подсказка-название у поля без подписи, причина недоступности действия. */
  always?: boolean;
  /** Класс обёртки — когда она участвует в раскладке строки. */
  className?: string;
  children: React.ReactNode;
}

function isCut(container: HTMLElement): boolean {
  return [container, ...container.querySelectorAll<HTMLElement>('*')].some(
    (element) => element.scrollWidth > element.clientWidth,
  );
}

/**
 * Подсказка с полным названием (концепция §6: «одна строка с многоточием и `Hint` с полным текстом»).
 * Показывается, только когда название действительно обрезано: иначе подсказка без нужды закрывала бы
 * соседние строки. По фокусу — только при переходе с клавиатуры.
 */
export function TitleHint({ text, pos, maxWidth, always = false, className, children }: TitleHintProps) {
  const [opened, setOpened] = useState(false);
  return (
    <Hint text={text} pos={pos} maxWidth={maxWidth} manual opened={opened}>
      <span
        className={className}
        onMouseEnter={(event) => setOpened(always || isCut(event.currentTarget))}
        onMouseLeave={() => setOpened(false)}
        onFocus={(event) => setOpened(event.target.matches(':focus-visible') && (always || isCut(event.currentTarget)))}
        onBlur={() => setOpened(false)}
      >
        {children}
      </span>
    </Hint>
  );
}
