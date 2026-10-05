/** Правила и тексты документов базы знаний (концепция §5.6). Чистые функции. */
import { isApiError, NetworkError } from '../api/client';
import type { DuplicateDocument, KbDocument, PortalConfig } from '../api/types';
import { errorText, formatDate, shortName, texts } from '../texts';

const t = texts.kb;
const BYTES_IN_MEGABYTE = 1024 * 1024;

/** Причины, при которых повторная обработка может помочь; остальные — в самом файле. */
const RETRYABLE = new Set(['recognition_failed', 'internal_error']);
/** Причины в самом файле: тот же файл закончится той же ошибкой. */
const FILE_REASONS = new Set(['file_unreadable', 'no_text', 'document_too_long']);

/** Есть ли у документа пункт «Обработать заново». */
export function canRetry(document: KbDocument): boolean {
  return document.status === 'error' && document.can_delete && RETRYABLE.has(document.error_code ?? '');
}

/** Причина ошибки документа по-человечески; вторая фраза зависит от права на удаление. */
export function documentErrorText(document: KbDocument): string {
  const [what, action] = t.errors[document.error_code ?? ''] ?? t.errors.internal_error ?? ['', ''];
  return `${what} ${document.can_delete ? action : t.askOwner}`;
}

/** Идёт ли обработка: пока такие документы есть в списке, он обновляется сам. */
export function isInProgress(document: KbDocument): boolean {
  return document.status === 'queued' || document.status === 'processing';
}

type KbLimits = PortalConfig['kb'];

export function megabytes(bytes: number): number {
  return Math.floor(bytes / BYTES_IN_MEGABYTE);
}

/** Проверка файла до загрузки: расширение и размер. Возвращает текст ошибки или `null`. */
export function checkKbFile(file: File, limits: KbLimits): string | null {
  const name = file.name.toLowerCase();
  if (!limits.document_extensions.some((extension) => name.endsWith(extension))) {
    return t.upload.unsupported;
  }
  return file.size > limits.document_max_bytes ? t.upload.tooLarge(megabytes(limits.document_max_bytes)) : null;
}

function duplicateText(existing: DuplicateDocument): string {
  const author = shortName(existing.author_full_name);
  const date = formatDate(existing.created_at);
  if (existing.status !== 'error') {
    return t.upload.duplicate(existing.title, author, date);
  }
  if (!existing.can_delete) {
    return t.upload.duplicateErrorOther(existing.title, author, date);
  }
  // Неизвестная причина — как `internal_error`: повторная обработка может помочь.
  return FILE_REASONS.has(existing.error_code ?? '')
    ? t.upload.duplicateErrorReplace(existing.title)
    : t.upload.duplicateErrorRetry(existing.title);
}

/** Текст ошибки загрузки одного файла. */
export function kbUploadErrorText(error: unknown, limits: KbLimits | null): string {
  if (error instanceof NetworkError) {
    return t.upload.network;
  }
  if (isApiError(error, 'duplicate_document') && error.details.document) {
    return duplicateText(error.details.document as DuplicateDocument);
  }
  if (isApiError(error, 'file_too_large')) {
    const max = typeof error.details.max_bytes === 'number' ? error.details.max_bytes : limits?.document_max_bytes;
    // Предел неизвестен (настройки не загрузились) — текст сервера.
    return max === undefined ? errorText(error) : t.upload.tooLarge(megabytes(max));
  }
  if (isApiError(error, 'unsupported_file_type')) {
    return t.upload.unsupported;
  }
  if (isApiError(error, 'file_unreadable')) {
    return t.upload.unreadable;
  }
  if (isApiError(error, 'too_many_pages')) {
    const max = typeof error.details.max_pages === 'number' ? error.details.max_pages : limits?.document_max_pages;
    return max === undefined ? errorText(error) : t.upload.tooManyPages(max);
  }
  return errorText(error);
}
