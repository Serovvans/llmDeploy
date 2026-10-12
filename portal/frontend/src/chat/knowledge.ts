/** Выбор баз знаний в панели запроса чата (концепция §4.1): порядок, подпись кнопки, запоминание в браузере. */
import type { KbScope } from '../api/types';
import { texts } from '../texts';

const t = texts.chat.composer.knowledge;

/** Порядок отметок в панели, в подписи кнопки и в запросе: «Общая», «Моя», «Министерства». */
export const KB_SCOPES: KbScope[] = ['shared', 'personal', 'ministries'];

const STORAGE_KEY = 'portal.chat.collections';
/** Выбор, запомненный до версии 0.15: одно значение из трёх. */
const LEGACY_STORAGE_KEY = 'portal.chat.knowledge';
const LEGACY: Record<string, KbScope[]> = { shared: ['shared'], shared_and_personal: ['shared', 'personal'] };

/** Приводит набор к порядку панели; посторонние значения отбрасываются. */
export function orderScopes(chosen: readonly string[]): KbScope[] {
  return KB_SCOPES.filter((scope) => chosen.includes(scope));
}

/** Последний выбор баз; по умолчанию — ни одной. Теги в браузере не хранятся (концепция, Д-20). */
export function readCollections(): KbScope[] {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored !== null) {
      return orderScopes(stored.split(','));
    }
    return LEGACY[window.localStorage.getItem(LEGACY_STORAGE_KEY) ?? ''] ?? [];
  } catch {
    // Хранилище недоступно — база знаний по умолчанию не используется.
    return [];
  }
}

export function storeCollections(collections: KbScope[]): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, collections.join(','));
  } catch {
    // Хранилище недоступно — выбор действует до закрытия вкладки.
  }
}

/**
 * Подпись кнопки: «не используется» либо названия отмеченных баз и число тегов.
 * `separator` перед числом тегов: на экране — « · », в имени для экранного чтения — «, ».
 */
export function knowledgeChoice(collections: KbScope[], tagCount: number, separator: string): string {
  if (collections.length === 0) {
    return t.notUsed;
  }
  const scopes = collections.map((scope) => t.scopes[scope]).join(', ');
  return tagCount > 0 ? `${scopes}${separator}${t.tagsCount(tagCount)}` : scopes;
}
