/** Проверки и тексты вложений чата (концепция §5.5). Окончательно всё решает сервер. */
import { isApiError } from '../api/client';
import type { PortalConfig } from '../api/types';
import { errorText, texts } from '../texts';

type ChatLimits = PortalConfig['chat'];

const BYTES_IN_MEGABYTE = 1024 * 1024;

function megabytes(bytes: number): number {
  return Math.floor(bytes / BYTES_IN_MEGABYTE);
}

/** Подсказка на кнопке «Прикрепить»: пределы названы до выбора файла. */
export function attachHint(limits: ChatLimits): string {
  return texts.chat.composer.attachHint(
    limits.attachment_max_pages,
    megabytes(limits.attachment_max_bytes),
    limits.max_attachments,
  );
}

/** Проверка файла в браузере до загрузки: расширение и размер. Возвращает текст отказа или `null`. */
export function checkFile(file: File, limits: ChatLimits): string | null {
  const name = file.name.toLowerCase();
  if (!limits.attachment_extensions.some((extension) => name.endsWith(extension))) {
    return texts.chat.files.unsupported(file.name);
  }
  if (file.size > limits.attachment_max_bytes) {
    return texts.chat.files.tooLarge(file.name, megabytes(limits.attachment_max_bytes));
  }
  return null;
}

function detail(error: unknown, key: string): number | null {
  const value = isApiError(error) ? error.details[key] : null;
  return typeof value === 'number' ? value : null;
}

/** Текст отказа сервера при загрузке файла. */
export function uploadErrorText(error: unknown, fileName: string, limits: ChatLimits): string {
  const files = texts.chat.files;
  if (isApiError(error, 'unsupported_file_type')) {
    return files.unsupported(fileName);
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
export function sendErrorText(error: unknown, config: PortalConfig): string {
  if (isApiError(error, 'message_too_long')) {
    return texts.chat.errors.tooLong;
  }
  if (isApiError(error, 'generation_in_progress')) {
    return texts.chat.errors.inProgress;
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
