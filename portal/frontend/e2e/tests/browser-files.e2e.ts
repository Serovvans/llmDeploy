/**
 * Поведение самого браузера: просмотр оригинала документа в новой вкладке и скачивание
 * файлов (docs/portal-api.md §8.2, «проверить на стенде»). Тесты с меткой @браузер идут ещё и
 * в установленном Chrome: у него, в отличие от сборки Chromium без окна, есть просмотр PDF.
 */
import type { Download, Page } from '@playwright/test';

import { uniqueSuffix } from '../support/accounts';
import { docxText, pngImage, textFile, textPdf, type TestFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { addKbDocument, askChat, createDialog, stubReply, waitKbReady } from '../support/portal';
import { attachFiles } from '../support/ui';

/** Открывает документ личной базы в просмотре и возвращает ссылку «Открыть оригинал». */
async function openViewer(page: Page, file: TestFile) {
  await page.getByText('Моя', { exact: true }).click();
  await page.getByRole('button', { name: file.name, exact: true }).click();
  const link = page.getByRole('dialog').getByRole('link', { name: 'Открыть оригинал' });
  await expect(link).toBeVisible();
  return link;
}

/**
 * Скачивание, начатое ссылкой с `target="_blank"`: браузер сообщает о нём либо в исходной вкладке,
 * либо в новой, которая тут же закрывается. Подписка на новую вкладку ставится в момент её появления.
 */
function nextDownload(page: Page): Promise<Download> {
  return new Promise((resolve) => {
    page.once('download', resolve);
    page.context().once('page', (tab) => tab.once('download', resolve));
  });
}

function twoPagePdf(mark: string): TestFile {
  return textPdf(`reglament-${mark}.pdf`, [
    `First page ${mark}a with general provisions of the regulation.`,
    `Second page ${mark}b about the lease term of the parcel.`,
  ]);
}

async function memberWithDocument(newMember: () => Promise<Member>, file: TestFile) {
  const member = await newMember();
  const document = await addKbDocument(member.api, file, { scope: 'personal' });
  return { member, document };
}

test('оригинал PDF: ссылка ведёт в новую вкладку на нужную страницу, сервер отдаёт PDF для показа @браузер', async ({
  newMember,
  pageAs,
}) => {
  const mark = `zq${uniqueSuffix()}`;
  const pdf = twoPagePdf(mark);
  const { member, document } = await memberWithDocument(newMember, pdf);
  const page = await pageAs(member, '/knowledge');
  const link = await openViewer(page, pdf);

  await page.getByRole('dialog').getByRole('button', { name: 'Следующая страница' }).click();
  await expect(page.getByRole('dialog')).toContainText('Страница 2 из 2');
  await expect(link).toHaveAttribute('href', `/api/kb/documents/${document.id}/file#page=2`);
  await expect(link).toHaveAttribute('target', '_blank');
  await expect(link).toHaveAttribute('rel', /noopener/);

  // Запрос из самого браузера, с его cookie.
  const response = await page.request.get(`/api/kb/documents/${document.id}/file`);
  expect(response.status()).toBe(200);
  const headers = response.headers();
  expect(headers['content-type']).toBe('application/pdf');
  expect(headers['content-disposition']).toMatch(/^inline; /);
  expect(decodeURIComponent(headers['content-disposition'] ?? '')).toContain(pdf.name);
  expect(headers['x-content-type-options']).toBe('nosniff');
  expect(headers['cache-control']).toBe('no-store');
  expect((await response.body()).equals(pdf.buffer)).toBe(true);
});

test('оригинал PDF показывается встроенным просмотром браузера при заголовках безопасности @браузер', async ({
  newMember,
  pageAs,
  browserName,
}, testInfo) => {
  test.skip(
    testInfo.project.name === 'chromium',
    'У сборки Chromium без окна нет просмотра PDF; то же проверяет проект chrome',
  );
  test.skip(browserName === 'webkit', 'В сборке WebKit для Playwright нет просмотра PDF: Safari проверяется вручную');

  const mark = `zq${uniqueSuffix()}`;
  const pdf = twoPagePdf(mark);
  const { member } = await memberWithDocument(newMember, pdf);
  const page = await pageAs(member, '/knowledge');
  const link = await openViewer(page, pdf);
  await page.getByRole('dialog').getByRole('button', { name: 'Следующая страница' }).click();
  await expect(page.getByRole('dialog')).toContainText('Страница 2 из 2');

  const opened = page.context().waitForEvent('page');
  await link.click();
  const viewer = await opened;
  const problems: string[] = [];
  viewer.on('console', (message) => {
    if (message.type() === 'error' || /Content.Security.Policy/i.test(message.text())) {
      problems.push(message.text());
    }
  });
  viewer.on('pageerror', (error) => problems.push(String(error)));

  await expect(viewer).toHaveURL(/\/file#page=2$/);
  await expect.poll(() => viewer.evaluate(() => document.contentType)).toBe('application/pdf');
  if (browserName === 'firefox') {
    // Просмотр Firefox — обычная страница: видно число страниц, текущую страницу и текст.
    await expect(viewer.locator('#viewer .page')).toHaveCount(2);
    await expect(viewer.locator('#pageNumber')).toHaveValue('2');
    await expect(viewer.locator('#viewer')).toContainText(`Second page ${mark}b`);
  }
  await viewer.screenshot({ path: testInfo.outputPath('оригинал-pdf.png') });
  expect(problems).toEqual([]);
  // Вкладка с оригиналом не получает доступа к вкладке портала.
  expect(await viewer.evaluate(() => window.opener)).toBeNull();
});

test('оригинал изображения и текста открывается в новой вкладке, русский текст читается @браузер', async ({
  newMember,
  pageAs,
}) => {
  const member = await newMember();
  const mark = uniqueSuffix();
  const scan = pngImage(`скан-${mark}.png`, mark);
  const note = textFile(`заметка-${mark}.md`, `# Заметка ${mark}\n\nСрок аренды участка — сорок девять лет.`);
  await addKbDocument(member.api, scan, { scope: 'personal' });
  await addKbDocument(member.api, note, { scope: 'personal' });
  const page = await pageAs(member, '/knowledge');

  let opened = page.context().waitForEvent('page');
  await (await openViewer(page, scan)).click();
  const image = await opened;
  await expect.poll(() => image.evaluate(() => document.contentType)).toBe('image/png');
  await expect.poll(() => image.evaluate(() => document.images[0]?.naturalWidth)).toBe(32);
  await image.close();
  await page.getByRole('dialog').getByRole('button', { name: 'Закрыть модальное окно' }).click();

  // Markdown отдаётся обычным текстом, а не разметкой: HTML из документа не исполняется.
  opened = page.context().waitForEvent('page');
  await page.getByRole('button', { name: note.name, exact: true }).click();
  await page.getByRole('dialog').getByRole('link', { name: 'Открыть оригинал' }).click();
  const text = await opened;
  await expect.poll(() => text.evaluate(() => document.contentType)).toBe('text/plain');
  await expect(text.locator('body')).toContainText('Срок аренды участка — сорок девять лет.');
});

test('DOCX: экспорт портала загружается в базу знаний, а его оригинал скачивается файлом @браузер', async ({
  newMember,
  pageAs,
}) => {
  const member = await newMember();
  const mark = `zq${uniqueSuffix()}`;
  const dialogId = await createDialog(member.api, 'chat');
  await askChat(member.api, dialogId, `Вопрос ${stubReply(`## Справка ${mark}\n\nСрок аренды — сорок девять лет.`)}`);
  const exported = await (await member.api.get(`/api/dialogs/${dialogId}/export`)).body();

  // Файл, который портал выдал как DOCX, он же принимает как DOCX и читает его текст.
  const docx: TestFile = {
    name: `справка-${mark}.docx`,
    mimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    buffer: exported,
  };
  const uploaded = await member.api.post('/api/kb/documents', {
    multipart: { file: docx, scope: 'personal', is_cogis: 'false' },
  });
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const document = await uploaded.json();
  await waitKbReady(member.api, document.id);
  expect(document.page_count).toBeNull();
  const text = await (await member.api.get(`/api/kb/documents/${document.id}/text`)).json();
  expect(text.segments.map((segment: { text: string }) => segment.text).join('')).toContain(`Справка ${mark}`);

  const page = await pageAs(member, '/knowledge');
  const link = await openViewer(page, docx);
  const download = nextDownload(page);
  await link.click();
  const file = await download;
  expect(file.suggestedFilename()).toBe(docx.name);
  const chunks: Buffer[] = [];
  for await (const chunk of await file.createReadStream()) {
    chunks.push(chunk as Buffer);
  }
  expect(docxText(Buffer.concat(chunks))).toContain(`Справка ${mark}`);
});

test('вложение-изображение показывается миниатюрой с адресом своего origin @браузер', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const mark = uniqueSuffix();
  const image = pngImage(`фото-${mark}.png`, mark);
  const page = await pageAs(member, '/chat');
  await attachFiles(page, image);
  const thumbnail = page.locator('main img').first();
  await expect.poll(() => thumbnail.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(32);
  // Миниатюра берётся адресом своего origin, без data: и blob: (политика содержимого).
  await expect(thumbnail).toHaveAttribute('src', /^\/api\/dialogs\/[0-9a-f-]+\/attachments\/[0-9a-f-]+\/file$/);
});
