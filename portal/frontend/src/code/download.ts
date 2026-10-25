const REVOKE_DELAY_MS = 60_000;

/** Сохранить готовый файл: `blob:` — только для скачивания, не для показа содержимого (концепция §12). */
export function saveBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = fileName;
  // Ссылка на время нажатия стоит в документе, адрес отзывается с задержкой — как у DOCX.
  document.body.append(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
}
