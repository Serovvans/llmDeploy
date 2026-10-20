/**
 * Открыта ли панель памяти: одно положение на все виды диалога и все ширины окна, при первом входе —
 * открыта (концепция §3.2, Д-30). Хранится в браузере, как тема.
 */
export const MEMORY_PANEL_KEY = 'portal.memoryPanel';

export function readPanelOpen(): boolean {
  try {
    return window.localStorage.getItem(MEMORY_PANEL_KEY) !== 'hidden';
  } catch {
    // Хранилище недоступно — как при первом входе.
    return true;
  }
}

export function storePanelOpen(open: boolean): void {
  try {
    window.localStorage.setItem(MEMORY_PANEL_KEY, open ? 'open' : 'hidden');
  } catch {
    // Хранилище недоступно — положение действует до закрытия вкладки.
  }
}
