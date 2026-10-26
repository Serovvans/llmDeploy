/**
 * Настройки интерфейса раздела «Код» в браузере (концепция §5.13.2, Д-43, Д-53): видна ли колонка
 * помощника, открытые вкладки, выбор «Видит» и «Данные слоя» предпросмотра по проектам. Только положение
 * интерфейса — не данные.
 */
const ASSISTANT_KEY = 'portal.codeAssistant';
const TABS_KEY = 'portal.codeTabs.';
const SCOPE_KEY = 'portal.codeScope.';

function read(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Хранилище недоступно — как при первом входе.
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Хранилище недоступно — выбор действует до закрытия вкладки.
  }
}

/** При первом входе колонка помощника открыта. */
export function readAssistantOpen(): boolean {
  return read(ASSISTANT_KEY) !== 'hidden';
}

export function storeAssistantOpen(open: boolean): void {
  write(ASSISTANT_KEY, open ? 'open' : 'hidden');
}

export interface StoredTabs {
  open: string[];
  active: string | null;
}

export function readTabs(projectId: string): StoredTabs | null {
  const raw = read(TABS_KEY + projectId);
  if (!raw) {
    return null;
  }
  try {
    const value = JSON.parse(raw) as Partial<StoredTabs>;
    const open = Array.isArray(value.open) ? value.open.filter((item): item is string => typeof item === 'string') : [];
    return { open, active: typeof value.active === 'string' ? value.active : null };
  } catch {
    return null;
  }
}

export function storeTabs(projectId: string, tabs: StoredTabs): void {
  write(TABS_KEY + projectId, JSON.stringify(tabs));
}

export type Scope = 'all' | 'open';

export function readScope(projectId: string): Scope {
  return read(SCOPE_KEY + projectId) === 'open' ? 'open' : 'all';
}

export function storeScope(projectId: string, scope: Scope): void {
  write(SCOPE_KEY + projectId, scope);
}

const LAYER_KEY = 'portal.codePreviewLayer.';

/** «Данные слоя» предпросмотра (§5.13.21): имя таблицы `схема.таблица`; `null` — придуманные объекты. */
export function readPreviewLayer(projectId: string): string | null {
  return read(LAYER_KEY + projectId);
}

export function storePreviewLayer(projectId: string, table: string | null): void {
  if (table === null) {
    try {
      window.localStorage.removeItem(LAYER_KEY + projectId);
    } catch {
      // Хранилище недоступно — выбор действует до закрытия вкладки.
    }
    return;
  }
  write(LAYER_KEY + projectId, table);
}
