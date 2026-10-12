/** Проверки и тексты вложений чата (концепция §5.5). Окончательно всё решает сервер. */
import { isApiError, NetworkError } from '../api/client';
import type { Attachment, DialogKind, PortalConfig } from '../api/types';
import { errorText, texts } from '../texts';

type ChatLimits = PortalConfig['chat'];
type MediaLimits = PortalConfig['media'];

const BYTES_IN_MEGABYTE = 1024 * 1024;
const BYTES_IN_GIGABYTE = 1024 * BYTES_IN_MEGABYTE;

function megabytes(bytes: number): number {
  return Math.floor(bytes / BYTES_IN_MEGABYTE);
}

/** Предел размера записи словами: «2 ГБ», меньше гигабайта — «500 МБ». */
export function mediaSize(bytes: number): string {
  return bytes >= BYTES_IN_GIGABYTE
    ? `${(Math.floor((bytes / BYTES_IN_GIGABYTE) * 10) / 10).toLocaleString('ru-RU')} ГБ`
    : `${megabytes(bytes)} МБ`;
}

function hasExtension(fileName: string, extensions: string[]): boolean {
  const name = fileName.toLowerCase();
  return extensions.some((extension) => name.endsWith(extension));
}

/** Запись ли это (аудио, видео): каким маршрутом загружать файл, решает расширение (концепция §5.5). */
export function isMediaFile(file: File, media: MediaLimits): boolean {
  return media.enabled && hasExtension(file.name, media.extensions);
}

/** Запись среди вложений — по типу содержимого, который определил сервер (контракт §5.1). */
export function isMediaAttachment(attachment: Attachment): boolean {
  return attachment.media_type.startsWith('audio/') || attachment.media_type.startsWith('video/');
}

/** Расширения для окна выбора файлов: записи предлагаются, только когда их приём включён. */
export function attachExtensions(config: PortalConfig): string[] {
  return [...config.chat.attachment_extensions, ...(config.media.enabled ? config.media.extensions : [])];
}

/** Подсказка на кнопке «Прикрепить»: пределы названы до выбора файла. */
export function attachHint(config: PortalConfig): string {
  return texts.chat.composer.attachHint(
    config.chat.attachment_max_pages,
    megabytes(config.chat.attachment_max_bytes),
    config.chat.max_attachments,
    config.media.enabled ? config.media.chat_max_minutes : null,
  );
}

/** Проверка файла в браузере до загрузки: расширение и размер. Возвращает текст отказа или `null`. */
export function checkFile(file: File, config: PortalConfig): string | null {
  if (isMediaFile(file, config.media)) {
    return file.size > config.media.max_bytes
      ? texts.chat.media.tooLarge(file.name, mediaSize(config.media.max_bytes))
      : null;
  }
  if (!hasExtension(file.name, config.chat.attachment_extensions)) {
    return texts.chat.files.unsupported(file.name, config.media.enabled);
  }
  if (file.size > config.chat.attachment_max_bytes) {
    return texts.chat.files.tooLarge(file.name, megabytes(config.chat.attachment_max_bytes));
  }
  return null;
}

function detail(error: unknown, key: string): number | null {
  const value = isApiError(error) ? error.details[key] : null;
  return typeof value === 'number' ? value : null;
}

/** Причина ошибки записи у вложения; `retryable` — файл ещё в браузере, загрузку можно начать заново. */
export interface MediaFailure {
  text: string;
  retryable: boolean;
}

/** Отказ при загрузке записи (концепция §5.5). Сам интерфейс загрузку не повторяет. */
export function mediaUploadFailure(error: unknown, fileName: string, media: MediaLimits | null): MediaFailure {
  const t = texts.chat.media;
  if (error instanceof NetworkError) {
    return { text: t.uploadBroken, retryable: true };
  }
  if (isApiError(error, 'too_many_uploads')) {
    return { text: t.tooManyUploads, retryable: true };
  }
  if (isApiError(error, 'upload_timeout')) {
    // Причин две, интерфейс их не различает: текст сервера как есть.
    return { text: errorText(error), retryable: true };
  }
  if (isApiError(error, 'unsupported_file_type')) {
    return { text: t.invalid(fileName), retryable: false };
  }
  const maxBytes = detail(error, 'max_bytes') ?? media?.max_bytes;
  if (isApiError(error, 'file_too_large') && maxBytes !== undefined) {
    return { text: t.tooLarge(fileName, mediaSize(maxBytes)), retryable: false };
  }
  if (isApiError(error, 'media_quota_exceeded')) {
    return { text: error.details.reason === 'user' ? t.quotaUser : t.quotaTotal, retryable: false };
  }
  return { text: errorText(error), retryable: false };
}

/** Причина ошибки расшифровки по `transcription.error_code`; неизвестный код — общий текст (концепция §5.5). */
export function transcriptionErrorText(code: string | null, fileName: string, media: MediaLimits | null): string {
  const t = texts.chat.media;
  switch (code) {
    case 'media_too_long':
      // Без конфигурации (она не загрузилась) предел неизвестен — общий текст.
      return media ? t.tooLong(fileName, media.chat_max_minutes) : t.failed(fileName);
    case 'media_invalid':
      return t.invalid(fileName);
    case 'media_no_audio':
      return t.noAudio(fileName);
    case 'no_text':
      return t.noSpeech(fileName);
    default:
      return t.failed(fileName);
  }
}

/** Текст отказа сервера при загрузке документа или изображения. */
export function uploadErrorText(error: unknown, fileName: string, config: PortalConfig): string {
  const files = texts.chat.files;
  const limits: ChatLimits = config.chat;
  if (isApiError(error, 'unsupported_file_type')) {
    return files.unsupported(fileName, config.media.enabled);
  }
  if (isApiError(error, 'file_too_large')) {
    return files.tooLarge(fileName, megabytes(detail(error, 'max_bytes') ?? limits.attachment_max_bytes));
  }
  if (isApiError(error, 'too_many_images')) {
    return files.scanTooLong(fileName, detail(error, 'max_images') ?? limits.max_images);
  }
  if (isApiError(error, 'too_many_pages')) {
    return files.tooManyPages(fileName, detail(error, 'max_pages') ?? limits.attachment_max_pages);
  }
  if (isApiError(error, 'file_unreadable')) {
    return files.unreadable(fileName);
  }
  return errorText(error);
}

/** Текст отказа до открытия потока ответа — под панелью запроса. */
export function sendErrorText(error: unknown, config: PortalConfig, kind: DialogKind): string {
  if (isApiError(error, 'message_too_long')) {
    return texts.chat.errors.tooLong;
  }
  if (isApiError(error, 'schema_not_found')) {
    return texts.sql.schemaDeleted;
  }
  if (isApiError(error, 'generation_in_progress')) {
    return texts.dialogs[kind].inProgress;
  }
  if (isApiError(error, 'attachment_not_ready')) {
    return texts.chat.composer.transcribing;
  }
  if (isApiError(error, 'too_many_images')) {
    return texts.chat.files.tooManyImages(detail(error, 'max_images') ?? config.chat.max_images);
  }
  if (isApiError(error, 'validation_error') && error.fields.some((f) => f.field === 'content' && f.code === 'too_long')) {
    return texts.chat.composer.messageTooLong(config.dialogs.message_max_chars);
  }
  return errorText(error);
}

/** Текст заметки об ошибке ответа по коду события `error` или `error_code` (концепция §5.5). */
export function answerErrorText(code: string | null, hasText: boolean, serverMessage: string | undefined): string {
  const errors = texts.chat.errors;
  switch (code) {
    case 'model_unavailable':
    case 'internal_error':
      return hasText ? errors.broken : errors.noAnswer;
    case 'interrupted':
    case 'session_ended':
      return errors.broken;
    case 'model_overloaded':
      return errors.overloaded;
    case 'generation_timeout':
      return errors.timeout;
    case 'knowledge_unavailable':
      return errors.knowledge;
    case 'message_too_long':
      return errors.tooLong;
    default:
      // Кода нет в словаре: текст сервера из потока; после перезагрузки его нет — общий текст.
      return serverMessage || (hasText ? errors.broken : errors.noAnswer);
  }
}
