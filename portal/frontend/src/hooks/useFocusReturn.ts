import { useEffect, useRef } from 'react';

/**
 * Возвращает фокус на элемент, открывший окно, после его закрытия (концепция §9: фокус не теряется).
 * Элемент задаётся селектором, а не ссылкой: пункт меню, с которого открыли окно, к этому моменту уже удалён.
 */
export function useFocusReturn(isOpen: boolean): (openerSelector: string) => void {
  const opener = useRef<string | null>(null);

  useEffect(() => {
    if (!isOpen && opener.current) {
      document.querySelector<HTMLElement>(opener.current)?.focus();
      opener.current = null;
    }
  }, [isOpen]);

  return (openerSelector) => {
    opener.current = openerSelector;
  };
}
