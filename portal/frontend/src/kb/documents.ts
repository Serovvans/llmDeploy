/** Правила и тексты документов базы знаний (концепция §5.6). Чистые функции. */
import { isApiError, NetworkError } from '../api/client';
import type { DuplicateDocument, KbDocument, PortalConfig } from '../api/types';
import { isMediaFile, mediaSize } from '../chat/files';
import { errorText, formatDate, formatDuration, shortName, texts } from '../texts';

const t = texts.kb;
const BYTES_IN_MEGABYTE = 1024 * 1024;

/**
 * Причины в самом файле либо файла на сервере уже нет: повторная обработка не поможет.
 * Любая другая причина, в том числе неизвестная, — как `internal_error`: повтор может помочь.
 */
const FILE_REASONS = new Set([
  'file_unreadable',
  'no_text',
  'document_too_long',
  'media_invalid',
  'media_no_audio',
  'media_too_long',
  'source_expired',
]);

/** Показана ли отметка о неудавшемся обновлении обработки: только тому, кто может обработать заново. */
export function hasReprocessFailure(document: KbDocument): boolean {
  return document.status === 'ready' && document.can_delete && document.reprocess_error !== null;
}

/** Есть ли у документа пункт «Обработать заново». */
export function canRetry(document: KbDocument): boolean {
  if (document.status === 'error') {
    return document.can_delete && !FILE_REASONS.has(document.error_code ?? '');
  }
  return hasReprocessFailure(document);
}

/** Причина ошибки документа по-человечески; вторая фраза зависит от права на удаление. */
export function documentErrorText(document: KbDocument, media: PortalConfig['media'] | null): string {
  const code = document.error_code ?? '';
  let reason = t.errors[code] ?? t.errors.internal_error ?? ['', ''];
  if (code === 'media_too_long') {
    reason = t.mediaTooLong(media?.max_minutes ?? null);
  } else if (code === 'no_text' && document.kind === 'recording') {
    reason = t.recordingNoText;
  }
  const [what, action] = reason;
  const ask = document.scope === 'ministries' ? t.askAdmin : t.askOwner;
  return `${what} ${document.can_delete ? action : ask}`;
}

/** Текст отметки о неудавшемся обновлении обработки по `reprocess_error`. */
export function reprocessFailureText(code: string): string {
  if (code === 'recognition_failed' || code === 'transcription_failed') {
    return t.reprocessFailed[code];
  }
  return t.reprocessFailed.other;
}

/** Столбец «Объём»: число страниц, у записи — длительность; `null` — нет ни того ни другого. */
export function documentVolume(document: KbDocument): string | null {
  if (document.duration_seconds !== null) {
    return formatDuration(document.duration_seconds);
  }
  return document.page_count === null ? null : t.pages(document.page_count);
}

/**
 * Идёт ли обработка: пока такие документы есть в списке, он обновляется сам. Сюда же — готовый документ,
 * чья обработка обновляется: иначе «Обработка обновляется» и отметка о повторном сбое устаревали бы.
 */
export function isInProgress(document: KbDocument): boolean {
  return document.status === 'queued' || document.status === 'processing' || document.reprocessing;
}

/** Ключ тега: «Аренда» и «аренда» — один тег (контракт §8.5). */
export function tagKey(tag: string): string {
  return tag.toLowerCase();
}

type TagLimits = PortalConfig['kb']['tags'];

/**
 * Список тегов после правки в поле: пробелы по краям отброшены, повторы по ключу слиты (остаётся первое
 * написание). Слишком длинный и лишний тег не добавляются — `error` называет причину. Без настроек
 * (`limits` равен `null`) пределы проверяет только сервер.
 */
export function acceptTags(next: string[], limits: TagLimits | null): { tags: string[]; error: string | null } {
  const tags: string[] = [];
  let error: string | null = null;
  for (const tag of next.map((item) => item.trim()).filter(Boolean)) {
    if (tags.some((item) => tagKey(item) === tagKey(tag))) {
      continue;
    }
    if (limits && tag.length > limits.max_length) {
      error = t.tags.tooLong(limits.max_length);
    } else if (limits && tags.length >= limits.max_per_document) {
      error = t.tags.tooMany(limits.max_per_document);
    } else {
      tags.push(tag);
    }
  }
  return { tags, error };
}

/** Отказ сервера по полю `tags` (контракт §8.5) — теми же текстами, что проверка в поле; иначе `null`. */
export function tagsErrorText(error: unknown, limits: TagLimits | null): string | null {
  const field = isApiError(error, 'validation_error') ? error.fields.find((item) => item.field === 'tags') : undefined;
  if (!field) {
    return null;
  }
  if (field.code === 'too_many' && limits) {
    return t.tags.tooMany(limits.max_per_document);
  }
  if (field.code === 'too_long' && limits) {
    return t.tags.tooLong(limits.max_length);
  }
  // Предел неизвестен (настройки не загрузились) — текст сервера.
  return field.code === 'invalid_format' ? t.tags.invalid : field.message;
}

export function megabytes(bytes: number): number {
  return Math.floor(bytes / BYTES_IN_MEGABYTE);
}

/** Запись ли это: записи уходят своим маршрутом, по одной и с ходом загрузки. Без настроек — не определить. */
export function isKbMedia(file: File, config: PortalConfig | null): boolean {
  return config !== null && isMediaFile(file, config.media);
}

/** Расширения для области выбора файлов: записи предлагаются, только когда их приём включён. */
export function kbExtensions(config: PortalConfig): string[] {
  return [...config.kb.document_extensions, ...(config.media.enabled ? config.media.extensions : [])];
}

/** Подсказка под областью выбора: пределы документов и, если записи принимаются, записей. */
export function kbLimitsHint(config: PortalConfig): string {
  const { kb, media } = config;
  const size = megabytes(kb.document_max_bytes);
  return media.enabled
    ? t.upload.limitsWithMedia(size, kb.document_max_pages, mediaSize(media.max_bytes), t.upload.mediaLength(media.max_minutes))
    : t.upload.limits(size, kb.document_max_pages);
}

/** Проверка файла до загрузки: расширение и размер. Возвращает текст ошибки или `null`. */
export function checkKbFile(file: File, config: PortalConfig): string | null {
  if (isKbMedia(file, config)) {
    return file.size > config.media.max_bytes ? t.upload.mediaTooLarge(mediaSize(config.media.max_bytes)) : null;
  }
  const name = file.name.toLowerCase();
  if (!config.kb.document_extensions.some((extension) => name.endsWith(extension))) {
    return t.upload.unsupported;
  }
  return file.size > config.kb.document_max_bytes ? t.upload.tooLarge(megabytes(config.kb.document_max_bytes)) : null;
}

/** Ход загрузки записи: «340 МБ из 1,2 ГБ · 28 %». Объём — доля размера файла по проценту отправленного. */
export function uploadProgressText(file: File, percent: number): string {
  return t.upload.progress(mediaSize((file.size * percent) / 100), mediaSize(file.size), percent);
}

function duplicateText(existing: DuplicateDocument, withTags: boolean): string {
  const author = shortName(existing.author_full_name);
  const date = formatDate(existing.created_at);
  if (existing.status !== 'error') {
    const text = t.upload.duplicate(existing.title, author, date);
    // Теги меняют у загруженного документа, а не повторной загрузкой — подсказка тому, кто вправе их менять.
    return withTags && existing.can_delete ? `${text}. ${t.upload.duplicateTags}` : text;
  }
  if (!existing.can_delete) {
    return t.upload.duplicateErrorOther(existing.title, author, date);
  }
  if (existing.error_code === 'source_expired') {
    return t.upload.duplicateSourceExpired(existing.title);
  }
  return FILE_REASONS.has(existing.error_code ?? '')
    ? t.upload.duplicateErrorReplace(existing.title)
    : t.upload.duplicateErrorRetry(existing.title);
}

/** Чем закончилась неудачная загрузка файла; `retry` — файл можно отправить снова, не выбирая заново. */
export interface KbUploadFailure {
  text: string;
  retry: boolean;
}

interface UploadContext {
  config: PortalConfig | null;
  /** Файл — запись: у неё свои пределы и свои отказы приёма (концепция §5.6). */
  media: boolean;
  /** В окне заданы теги — к тексту повтора добавляется подсказка, где их сменить. */
  withTags: boolean;
}

/**
 * Отказ в загрузке одного файла. Снова отправить можно файл, который не дошёл не по своей вине: обрыв связи,
 * занятые места загрузки, прерванный приём — и отказ, который исправляется в самом окне (теги, место).
 */
export function kbUploadFailure(error: unknown, { config, media, withTags }: UploadContext): KbUploadFailure {
  if (error instanceof NetworkError) {
    return { text: t.upload.network, retry: true };
  }
  const tagsError = tagsErrorText(error, config?.kb.tags ?? null);
  if (tagsError !== null) {
    return { text: tagsError, retry: true };
  }
  if (isApiError(error, 'forbidden')) {
    return { text: t.upload.ministriesForbidden, retry: true };
  }
  if (isApiError(error, 'too_many_uploads')) {
    return { text: texts.chat.media.tooManyUploads, retry: true };
  }
  if (isApiError(error, 'upload_timeout')) {
    // Причин две, интерфейс их не различает: текст сервера как есть.
    return { text: errorText(error), retry: true };
  }
  return { text: rejectionText(error, { config, media, withTags }), retry: false };
}

function rejectionText(error: unknown, { config, media, withTags }: UploadContext): string {
  if (isApiError(error, 'duplicate_document') && error.details.document) {
    return duplicateText(error.details.document as DuplicateDocument, withTags);
  }
  if (isApiError(error, 'file_too_large')) {
    const limit = media ? config?.media.max_bytes : config?.kb.document_max_bytes;
    const max = typeof error.details.max_bytes === 'number' ? error.details.max_bytes : limit;
    if (max === undefined) {
      // Предел неизвестен (настройки не загрузились) — текст сервера.
      return errorText(error);
    }
    return media ? t.upload.mediaTooLarge(mediaSize(max)) : t.upload.tooLarge(megabytes(max));
  }
  if (isApiError(error, 'unsupported_file_type')) {
    return media ? t.upload.mediaInvalid : t.upload.unsupported;
  }
  if (isApiError(error, 'media_quota_exceeded')) {
    return error.details.reason === 'user' ? t.upload.quotaUser : texts.chat.media.quotaTotal;
  }
  if (isApiError(error, 'file_unreadable')) {
    return t.upload.unreadable;
  }
  if (isApiError(error, 'too_many_pages')) {
    const max = typeof error.details.max_pages === 'number' ? error.details.max_pages : config?.kb.document_max_pages;
    return max === undefined ? errorText(error) : t.upload.tooManyPages(max);
  }
  return errorText(error);
}
