import { Hint } from '@skbkontur/react-ui';
import { useState } from 'react';

interface TitleHintProps {
  /** Полный текст названия. */
  text: string;
  pos?: React.ComponentProps<typeof Hint>['pos'];
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
export function TitleHint({ text, pos, children }: TitleHintProps) {
  const [opened, setOpened] = useState(false);
  return (
    <Hint text={text} pos={pos} manual opened={opened}>
      <span
        onMouseEnter={(event) => setOpened(isCut(event.currentTarget))}
        onMouseLeave={() => setOpened(false)}
        onFocus={(event) => setOpened(event.target.matches(':focus-visible') && isCut(event.currentTarget))}
        onBlur={() => setOpened(false)}
      >
        {children}
      </span>
    </Hint>
  );
}
