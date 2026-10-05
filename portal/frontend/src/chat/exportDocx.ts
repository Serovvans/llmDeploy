import { SingleToast } from '@skbkontur/react-ui';

import { api, isApiError } from '../api/client';
import { texts } from '../texts';

const t = texts.chat;
const inProgress = new Set<string>();
const REVOKE_DELAY_MS = 60_000;

/**
 * «Скачать в DOCX» (концепция §5.5): диалог целиком или один ответ `messageId`.
 * Пока файл готовится, повторное нажатие на ту же ссылку ничего не делает.
 */
export async function exportDocx(dialogId: string, messageId?: string): Promise<void> {
  const key = `${dialogId}:${messageId ?? ''}`;
  if (inProgress.has(key)) {
    return;
  }
  inProgress.add(key);
  SingleToast.push(t.exporting);
  try {
    const { blob, fileName } = await api.exportDialog(dialogId, messageId);
    // `blob:` — только для скачивания готового файла (концепция §12), не для показа содержимого.
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = fileName ?? t.exportFileName;
    // Ссылка на время нажатия стоит в документе, адрес отзывается с задержкой: иначе Firefox и Safari
    // могут не начать скачивание.
    document.body.append(link);
    link.click();
    link.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
  } catch (error) {
    if (isApiError(error, 'unauthenticated')) {
      return;
    }
    let text: string = t.exportFailed;
    if (isApiError(error, 'not_found')) {
      text = t.exportDeleted;
    } else if (isApiError(error, 'nothing_to_export')) {
      text = t.exportNothing;
    }
    SingleToast.push(text, { use: 'error' });
  } finally {
    inProgress.delete(key);
  }
}
