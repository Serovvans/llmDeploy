import { ThemeContext } from '@skbkontur/react-ui';
import { useContext, useLayoutEffect } from 'react';

import type { ResolvedTheme } from './preference';

type KonturTheme = React.ContextType<typeof ThemeContext>;

function mix(share: number, color: string, base: string): string {
  return `color-mix(in srgb, ${color} ${share}%, ${base})`;
}

/**
 * Переменные портала из токенов текущей темы — таблица концепции §7.2.
 * Правило для каждой переменной одно на обе темы: разницу дают сами токены.
 */
export function portalVariables(theme: KonturTheme): Record<string, string> {
  return {
    '--p-bg': theme.bgDefault,
    '--p-text': theme.textColorDefault,
    '--p-text-muted': mix(64, theme.textColorDefault, theme.bgDefault),
    '--p-surface-2': mix(4, theme.textColorDefault, theme.bgDefault),
    '--p-surface-3': mix(9, theme.textColorDefault, theme.bgDefault),
    '--p-line': theme.borderColorGrayLight,
    '--p-accent': theme.linkColor,
    '--p-focus': theme.borderColorFocus,
    '--p-error-text': mix(70, theme.errorText, theme.textColorDefault),
    '--p-error-bg': mix(20, theme.errorMain, theme.bgDefault),
    '--p-warning-bg': mix(20, theme.warningMain, theme.bgDefault),
    '--p-warning-icon': mix(55, theme.warningMain, theme.textColorDefault),
    '--p-mark-bg': mix(22, theme.linkColor, theme.bgDefault),
  };
}

/** Выставляет переменные портала на `<html>`; свои компоненты берут цвета только из них. */
export function ThemeBridge({ resolved }: { resolved: ResolvedTheme }) {
  const theme = useContext(ThemeContext);

  useLayoutEffect(() => {
    const root = document.documentElement;
    for (const [name, value] of Object.entries(portalVariables(theme))) {
      root.style.setProperty(name, value);
    }
    root.dataset.theme = resolved;
    root.style.colorScheme = resolved;
  }, [theme, resolved]);

  return null;
}
