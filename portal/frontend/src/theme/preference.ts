/** Выбор темы: хранится в браузере (концепция §7.1, Д-14). Ключ и значения совпадают с public/theme-init.js. */

export type ThemePreference = 'system' | 'light' | 'dark';
export type ResolvedTheme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'portal.theme';
export const SYSTEM_DARK_QUERY = '(prefers-color-scheme: dark)';

export function readPreference(): ThemePreference {
  try {
    const stored = window.localStorage.getItem(THEME_STORAGE_KEY);
    return stored === 'light' || stored === 'dark' ? stored : 'system';
  } catch {
    // Хранилище недоступно (закрытый режим браузера) — тема как в системе.
    return 'system';
  }
}

export function storePreference(preference: ThemePreference): void {
  try {
    if (preference === 'system') {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    // Хранилище недоступно — выбор действует до закрытия вкладки.
  }
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): ResolvedTheme {
  if (preference === 'system') {
    return systemDark ? 'dark' : 'light';
  }
  return preference;
}
