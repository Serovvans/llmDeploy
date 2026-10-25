import type { ResolvedTheme } from './preference';

/**
 * Шесть цветов подсветки кода на тему — единственные свои цвета портала (концепция §7.3). Их берут и
 * блоки кода в ленте (переменные `--p-code-*`), и редактор Monaco раздела «Код».
 */
export const CODE_COLORS: Record<ResolvedTheme, Record<'keyword' | 'string' | 'number' | 'type' | 'comment', string>> = {
  light: { keyword: '#0B5CAD', string: '#1D6F42', number: '#9A4A00', type: '#6F3FB5', comment: '#5F5F5F' },
  dark: { keyword: '#7DB7FF', string: '#7FD4A0', number: '#F0B070', type: '#C3A6FF', comment: '#ADADAD' },
};

export function codeVariables(theme: ResolvedTheme): Record<string, string> {
  return Object.fromEntries(Object.entries(CODE_COLORS[theme]).map(([name, value]) => [`--p-code-${name}`, value]));
}
