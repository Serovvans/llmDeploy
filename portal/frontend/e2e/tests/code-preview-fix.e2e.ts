/**
 * Правки 1.0 (docs/portal-api.md, изменение 0.25; §7.5.16, §7.5.17; docs/portal-ui.md §5.13.20–5.13.21):
 * родитель фрейма отбрасывает сообщения с чужим `file` и директивой не по шаблону; «Исправить с помощником»
 * по ошибкам предпросмотра шлёт `preview_fix`, текст вопроса собирает сервер; код изменился после запуска —
 * отказ сервера и `Toast`, вопроса нет. Слой из образца — в db-fixes.e2e.ts (нужен `db.enabled`).
 */
import type { Page, Request } from '@playwright/test';

import { saveFile } from '../support/code';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { toast } from '../support/ui';
import { createWebProject } from '../support/web';

const HEADER = 'Исправить ошибки, которые появились в предпросмотре:';

/** Открывает колонку предпросмотра и ждёт, пока на станции будет `errors` ошибок. */
async function previewWithErrors(page: Page, errors: number): Promise<void> {
  await page.getByRole('button', { name: 'Предпросмотр', exact: true }).first().click();
  await expect(page.getByRole('heading', { level: 2, name: 'Предпросмотр' })).toBeVisible();
  await expect(page.getByText(`Предпросмотр · ${errors}`, { exact: true })).toBeVisible({ timeout: 20_000 });
}

/** Ждёт вопрос «Исправить с помощником» диалога `dialogId`, нажимая кнопку станции «Предпросмотр». */
async function clickFix(page: Page, dialogId: string): Promise<Request> {
  const sent = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith(`/api/dialogs/${dialogId}/messages`));
  await page.getByRole('region', { name: 'Проверка кода' }).getByRole('button', { name: 'Исправить с помощником' }).click();
  return sent;
}

async function userMessages(page: Page, dialogId: string): Promise<{ content: string }[]> {
  const response = await page.request.get(`/api/dialogs/${dialogId}/messages`);
  expect(response.status()).toBe(200);
  return ((await response.json()).items as { role: string; content: string }[]).filter((item) => item.role === 'user');
}

test('сообщения фрейма с файлом не из проекта и директивой не по шаблону отбрасываются', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'html_widget', 'Подделка сообщений');
  await saveFile(
    member.api,
    project.id,
    must(project.files[0], 'файл шаблона'),
    [
      '<p>Подделка</p>',
      '<script>',
      "parent.postMessage({ type: 'error', file: 'evil.js', line: 1, column: 1, message: 'FORGED-FILE' }, '*');",
      "parent.postMessage({ type: 'error', file: 'widget.html ', line: 1, column: 1, message: 'FORGED-SPACE' }, '*');",
      "parent.postMessage({ type: 'blocked', url: 'https://example.com/FORGED-SEMICOLON', directive: 'connect-src; script-src' }, '*');",
      "parent.postMessage({ type: 'blocked', url: 'https://example.com/FORGED-CASE', directive: 'Connect-Src' }, '*');",
      `parent.postMessage({ type: 'blocked', url: 'https://example.com/FORGED-LONG', directive: '${'a'.repeat(65)}' }, '*');`,
      "parent.postMessage({ type: 'blocked', url: 'https://example.com/FORGED-EMPTY', directive: '' }, '*');",
      // По шаблону ^[a-z-]{1,64}$, но не из закрытого списка директив.
      "parent.postMessage({ type: 'blocked', url: 'https://example.com/FORGED-UNLISTED', directive: 'connect-srcx' }, '*');",
      // Последним — сообщение по правилам: когда оно видно, прежние уже разобраны (порядок postMessage).
      "parent.postMessage({ type: 'error', file: 'widget.html', line: 2, column: 1, message: 'Uncaught RangeError: CONTROL-ERROR' }, '*');",
      '</script>',
    ].join('\n'),
  );
  const page = await pageAs(member, `/code/${project.id}`);
  await previewWithErrors(page, 1);
  await page.getByText('Ошибки · 1', { exact: true }).click();
  // Тексты ошибок вкладки — `code[lang="en"]` (как в code-preview.e2e.ts); в редакторе подделки остаются кодом.
  const errorTexts = page.locator('code[lang="en"]');
  await expect(errorTexts.getByText('Uncaught RangeError: CONTROL-ERROR', { exact: true })).toBeVisible();
  await page.waitForTimeout(500);
  await expect(errorTexts).toHaveText(['Uncaught RangeError: CONTROL-ERROR']);
  await expect(page.getByText(/^Ошибки · \d+$/)).toHaveText('Ошибки · 1');
  await expect(page.getByText(/^Предпросмотр · \d+$/)).toHaveText('Предпросмотр · 1');
  await expect(page.getByText(/Код обратился к .*FORGED/)).toHaveCount(0);
});

test('«Исправить с помощником» с придуманным слоем: preview_fix с текстом, вопрос с заголовком header', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'html_widget', 'Исправление предпросмотра');
  await saveFile(
    member.api,
    project.id,
    must(project.files[0], 'файл шаблона'),
    [
      '<p>Ошибки</p>',
      '<script>',
      "parent.postMessage({ type: 'error', file: 'widget.html', line: 3, column: 1, message: 'Uncaught RangeError: invented-detail' }, '*');",
      "parent.postMessage({ type: 'error', file: null, line: null, column: null, message: 'Uncaught FancyError: free-name' }, '*');",
      '</script>',
    ].join('\n'),
  );
  const page = await pageAs(member, `/code/${project.id}`);
  await previewWithErrors(page, 2);
  const body = (await clickFix(page, project.dialog_id)).postDataJSON();
  expect(body.content).toBeUndefined();
  expect(body.action).toBe('write');
  expect(body.preview_fix).toEqual({
    layer: 'invented',
    problems: [
      { kind: 'error', path: 'widget.html', line: 3, type: 'RangeError', text: 'Uncaught RangeError: invented-detail' },
      // Имя ошибки вне закрытого списка уходит как Error (§7.5.16).
      { kind: 'error', path: null, line: null, type: 'Error', text: 'Uncaught FancyError: free-name' },
    ],
  });
  const expected = [HEADER, 'widget.html, строка 3: Uncaught RangeError: invented-detail', 'место не определено: Uncaught FancyError: free-name'].join('\n');
  await expect.poll(async () => (await userMessages(page, project.dialog_id)).map((item) => item.content)).toEqual([expected]);
  await expect(page.getByText(HEADER)).toBeVisible();
  await expect(page.getByText('widget.html, строка 3: Uncaught RangeError: invented-detail')).toBeVisible();
});

test('код изменился после запуска предпросмотра: сервер отказывает, Toast, вопроса в ленте нет', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'html_widget', 'Код изменился');
  const file = await saveFile(
    member.api,
    project.id,
    must(project.files[0], 'файл шаблона'),
    ['<p>Строка 1</p>', '<p>Строка 2</p>', '<p>Строка 3</p>', '<script>', "parent.postMessage({ type: 'error', file: 'widget.html', line: 5, column: 1, message: 'Uncaught TypeError: x' }, '*');", '</script>'].join('\n'),
  );
  const page = await pageAs(member, `/code/${project.id}`);
  await previewWithErrors(page, 1);
  // Файл укоротили в другом месте (другая вкладка): строки 5 в сохранённом файле больше нет.
  await saveFile(member.api, project.id, file, '<p>Одна строка</p>');

  const response = page.waitForResponse((item) => item.request().method() === 'POST' && item.url().endsWith(`/api/dialogs/${project.dialog_id}/messages`));
  await page.getByRole('region', { name: 'Проверка кода' }).getByRole('button', { name: 'Исправить с помощником' }).click();
  expect((await response).status()).toBe(422);
  await expect(toast(page, 'Код изменился после запуска предпросмотра. Запустите его заново')).toBeVisible();
  await expect(page.getByText(HEADER)).toHaveCount(0);
  expect(await userMessages(page, project.dialog_id)).toEqual([]);
});

test('сообщения фрейма до ready не попадают в журнал и на вкладку «Ошибки»', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'html_widget', 'До готовности');
  await saveFile(member.api, project.id, must(project.files[0], 'файл шаблона'), '<p>После</p>\n<script>api.showAlert("CONTROL-AFTER", "x");</script>');
  const page = await pageAs(member, `/code/${project.id}`);
  // Страница предпросмотра, которая до `ready` шлёт запись журнала и ошибку: родитель их не берёт (§7.5.17).
  await page.context().route('**/code-preview/frame.html', async (route) => {
    // Без сжатия: Firefox иначе отдаёт route.fetch тело в zstd как есть.
    const response = await route.fetch({ headers: { ...route.request().headers(), 'accept-encoding': 'identity' } });
    const body = (await response.text()).replace(
      "send({ type: 'ready' });",
      "send({ type: 'log', fn: 'BEFORE_READY', args: [], known: true, count: null }); send({ type: 'error', file: null, line: null, column: null, message: 'BEFORE-READY' }); send({ type: 'ready' });",
    );
    expect(body).toContain('BEFORE_READY');
    const headers = Object.fromEntries(Object.entries(response.headers()).filter(([name]) => !['content-encoding', 'content-length'].includes(name)));
    await route.fulfill({ response, body, headers });
  });
  await page.getByRole('button', { name: 'Предпросмотр', exact: true }).first().click();
  const log = page.getByRole('list', { name: 'Журнал вызовов' });
  await expect(log.getByText(/api\.showAlert\("CONTROL-AFTER", "x"\)/)).toBeVisible({ timeout: 20_000 });
  await expect(log.getByText(/BEFORE_READY/)).toHaveCount(0);
  await expect(page.getByText(/^Ошибки · \d+$/)).toHaveText('Ошибки · 0');
});
