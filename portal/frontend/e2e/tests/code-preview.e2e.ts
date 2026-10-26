/**
 * Предпросмотр JS и HTML (docs/portal-design.md §10, критерий 40; docs/portal-api.md §7.5.17,
 * §13.1 — изоляция фрейма; CLAUDE.md, пайплайн «Код», правило 14): код во фрейме не читает cookie,
 * хранилища и DOM портала, не обращается к сети; вызовы `api` видны в журнале с аргументами,
 * ошибки — во вкладке «Ошибки»; заголовки страницы предпросмотра.
 */
import type { Page } from '@playwright/test';

import { randomBytes } from 'node:crypto';

import { saveFile } from '../support/code';
import { expect, test } from '../support/fixtures';
import { expectError } from '../support/portal';
import { caddyLog, restartApiWith } from '../support/stand';
import { createWebProject } from '../support/web';

/** Открывает колонку предпросмотра проекта. */
async function openPreview(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Предпросмотр', exact: true }).first().click();
  await expect(page.getByRole('heading', { level: 2, name: 'Предпросмотр' })).toBeVisible();
}

/** Записи журнала вызовов: текст каждой строки. */
async function logRows(page: Page): Promise<string[]> {
  return page.getByRole('list', { name: 'Журнал вызовов' }).getByRole('button').allInnerTexts();
}

/** Попытка выйти за пределы фрейма: итог пишется вызовом `api.probe(имя, итог)` в журнал. */
function probeWidget(mark: string): string {
  return `<div id="box">Проба</div>
<script>
(function () {
  function probe(name, action) {
    try { api.probe(name, 'ok:' + String(action())); } catch (error) { api.probe(name, 'err:' + error.name); }
  }
  probe('cookie', function () { return document.cookie; });
  probe('localStorage', function () { return localStorage.length; });
  probe('sessionStorage', function () { return sessionStorage.length; });
  probe('indexedDB', function () { var r = indexedDB.open('${mark}'); r.onsuccess = function () { api.probe('indexedDB-open', 'ok'); }; r.onerror = function () { api.probe('indexedDB-open', 'err'); }; return 'request'; });
  probe('parent', function () { return parent.document.title; });
  probe('top', function () { window.top.location.href = '/chat?${mark}-top'; return 'set'; });
  probe('open', function () { return window.open('/chat?${mark}-open'); });
  probe('fetchApi', function () {
    fetch('/api/auth/session?${mark}').then(function (r) { api.probe('fetchApi-result', 'status:' + r.status); }, function (e) { api.probe('fetchApi-result', 'err:' + e.name); });
    return 'started';
  });
  probe('fetchExternal', function () {
    fetch('https://example.com/${mark}').then(function (r) { api.probe('fetchExternal-result', 'status:' + r.status); }, function (e) { api.probe('fetchExternal-result', 'err:' + e.name); });
    return 'started';
  });
  probe('xhr', function () { var x = new XMLHttpRequest(); x.open('GET', '/api/auth/session?${mark}-xhr'); x.send(); return 'sent'; });
  probe('img', function () { var i = new Image(); i.src = 'https://example.com/${mark}.png'; document.body.appendChild(i); return 'added'; });
  probe('imgPortal', function () { var i = new Image(); i.src = '/${mark}-local.png'; document.body.appendChild(i); return 'added'; });
  probe('script', function () { var s = document.createElement('script'); s.src = '/${mark}.js'; document.body.appendChild(s); return 'added'; });
  probe('websocket', function () { return new WebSocket('wss://' + 'localhost:8443/${mark}'); });
  probe('rtc', function () { return new RTCPeerConnection({ iceServers: [{ urls: 'stun:example.com:3478' }] }); });
  probe('beacon', function () { return navigator.sendBeacon('/${mark}-beacon', 'x'); });
})();
</script>`;
}

test('изоляция фрейма: cookie, хранилища, портал и сеть недоступны коду предпросмотра', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const mark = `iso${randomBytes(6).toString('hex')}`;
  const project = await createWebProject(member.api, 'html_widget', 'Изоляция');
  const file = project.files[0];
  expect(file).toBeDefined();
  if (!file) return;
  await saveFile(member.api, project.id, file, probeWidget(mark));
  const since = new Date().toISOString();

  const page = await pageAs(member, `/code/${project.id}`);
  // Запрос, на который пришёл ответ, дошёл до сервера; запрет CSP браузер отдаёт как неудачу запроса.
  const answered: string[] = [];
  const failed: string[] = [];
  page.on('response', (response) => answered.push(response.url()));
  page.on('requestfailed', (request) => failed.push(`${request.url()} ${request.failure()?.errorText ?? ''}`));
  const startUrl = page.url();
  await openPreview(page);

  const results = new Map<string, string>();
  await expect
    .poll(
      async () => {
        for (const row of await logRows(page)) {
          const match = /api\.probe\("([^"]+)", "([^"]*)"\)/.exec(row);
          if (match?.[1] && match[2] !== undefined) {
            results.set(match[1], match[2]);
          }
        }
        return ['cookie', 'rtc', 'fetchApi-result', 'fetchExternal-result'].every((name) => results.has(name));
      },
      { timeout: 20_000 },
    )
    .toBe(true);

  const sessionCookies = (await page.context().cookies()).map((cookie) => cookie.value).filter((value) => value.length > 8);
  const cookie = results.get('cookie') ?? '';
  expect(cookie === 'ok:' || cookie.startsWith('err:'), `cookie: ${cookie}`).toBe(true);
  for (const value of sessionCookies) {
    expect(cookie).not.toContain(value);
  }
  expect(results.get('localStorage')).toMatch(/^err:/);
  expect(results.get('sessionStorage')).toMatch(/^err:/);
  expect(results.get('indexedDB-open') ?? results.get('indexedDB')).toMatch(/^err/);
  expect(results.get('parent')).toMatch(/^err:/);
  expect(results.get('top')).toMatch(/^err:/);
  expect(results.get('open')).toMatch(/^(ok:null|err:)/);
  expect(results.get('fetchApi-result')).toMatch(/^err:/);
  expect(results.get('fetchExternal-result')).toMatch(/^err:/);
  expect(results.get('rtc')).toMatch(/^err:/);
  expect(page.url()).toBe(startUrl);
  expect(page.context().pages()).toHaveLength(1);

  // Запреты видны сотруднику во вкладке «Ошибки».
  await page.getByText(/^Ошибки · \d+$/).click();
  await expect(page.getByText(/Код обратился к .*: в предпросмотре сеть закрыта/).first()).toBeVisible();

  // Ни браузер, ни Caddy не видели запросов с меткой: со страницы предпросмотра ушёл только frame.html.
  await page.waitForTimeout(1_000);
  expect(answered.filter((url) => url.includes(mark)), failed.join('\n')).toEqual([]);
  expect(answered.filter((url) => url.includes('/code-preview/'))).toEqual([expect.stringMatching(/\/code-preview\/frame\.html$/)]);
  expect(caddyLog(since)).not.toContain(mark);
});

test('журнал вызовов api с аргументами и ошибки выполнения кода видны сотруднику', async ({ newMember, pageAs, browserName }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'html_widget', 'Журнал');
  const file = project.files[0];
  if (!file) throw new Error('нет файла шаблона');
  await saveFile(
    member.api,
    project.id,
    file,
    [
      '<p>Журнал</p>',
      '<script>',
      'var layer = api.getLayer("https://cogis.example/layer", 3);',
      'api.goTo(api.createPoint(82.9246, 55.0304, 4326));',
      'api.searchInLayer(layer, "status = \'Аренда\'").then(function (items) { api.showAlert("Найдено", String(items.length)); });',
      'api.notInApi(1);',
      'missingFunction();',
      '</script>',
    ].join('\n'),
  );
  const page = await pageAs(member, `/code/${project.id}`);
  await openPreview(page);

  const log = page.getByRole('list', { name: 'Журнал вызовов' });
  await expect(log.getByRole('button', { name: /api\.getLayer\("https:\/\/cogis\.example\/layer", 3\)/ })).toBeVisible({ timeout: 15_000 });
  await expect(log.getByText('→ слой предпросмотра').first()).toBeVisible();
  await expect(log.getByText(/api\.createPoint\(82\.9246, 55\.0304, 4326\)/)).toBeVisible();
  await expect(log.getByText(/5 объектов — условие в предпросмотре не применяется/)).toBeVisible();
  await expect(log.getByText(/api\.showAlert\("Найдено", "5"\)/)).toBeVisible();
  await expect(log.getByText('такой функции нет в описании api CoGIS')).toBeVisible();
  // Окно showAlert нарисовано внутри фрейма.
  await expect(page.frameLocator('iframe[title="Предпросмотр: widget.html"]').getByText('Найдено')).toBeVisible();

  await page.getByText(/^Ошибки · [1-9]\d*$/).click();
  const errorText = page.locator('code[lang="en"]').first();
  if (browserName === 'webkit') {
    // WebKit отдаёт странице с непрозрачным origin только «Script error.» без места (см. отчёт приёмки).
    await expect(errorText).toHaveText(/missingFunction|Script error\./);
    return;
  }
  await expect(errorText).toContainText('missingFunction');
  await expect(page.getByRole('button', { name: /^widget\.html, строка \d+/ }).first()).toBeVisible();
});

test('JS-плагин из шаблона выполняется по «Запустить плагин»', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'js_plugin', 'Плагин');
  const page = await pageAs(member, `/code/${project.id}`);
  await openPreview(page);
  await expect(page.getByText('Нажмите «Запустить плагин» в предпросмотре')).toBeVisible({ timeout: 15_000 });
  await page.frameLocator('iframe[title="Предпросмотр: plugin.js"]').getByRole('button', { name: 'Запустить плагин' }).click();
  const log = page.getByRole('list', { name: 'Журнал вызовов' });
  await expect(log.getByText(/^api\.getLayer\(/).first()).toBeVisible();
  await expect(log.getByText(/^api\.searchInLayer\(/).first()).toBeVisible();
});

test('страница предпросмотра отдаётся со своей политикой: без сети, sandbox allow-scripts', async ({ newMember }) => {
  const member = await newMember();
  const frame = await member.api.get('/code-preview/frame.html');
  expect(frame.status()).toBe(200);
  const csp = frame.headers()['content-security-policy'] ?? '';
  for (const part of ["default-src 'none'", "connect-src 'none'", "img-src data:", "frame-ancestors 'self'", 'sandbox allow-scripts', "form-action 'none'"]) {
    expect(csp).toContain(part);
  }
  expect(csp).not.toMatch(/script-src[^;]*(self|https?:)/);
  expect(frame.headers()['x-frame-options']).toBe('SAMEORIGIN');
  expect(frame.headers()['referrer-policy']).toBe('no-referrer');
  // Других файлов под /code-preview/ нет.
  expect((await member.api.get('/code-preview/other.js')).status()).toBe(404);
  // Портал свою политику не ослабил.
  const portalCsp = (await member.api.get('/code')).headers()['content-security-policy'] ?? '';
  expect(portalCsp).toContain("default-src 'self'");
  expect(portalCsp).not.toContain('unsafe-eval');
});

/**
 * Не проверка, а наблюдение для концепции §5.13.21 «Зависший код»: исполняет ли браузер фрейм
 * предпросмотра в отдельном процессе. Итог — в пометке теста (`frame-process`).
 */
test('наблюдение: бесконечный цикл во фрейме и отзывчивость вкладки портала @браузер', async ({ newMember, pageAs, browserName }) => {
  test.setTimeout(120_000);
  const member = await newMember();
  const project = await createWebProject(member.api, 'html_widget', 'Зависание');
  const file = project.files[0];
  if (!file) throw new Error('нет файла шаблона');
  await saveFile(member.api, project.id, file, '<p>Цикл</p>\n<script>setTimeout(function () { for (;;) {} }, 3000);</script>');
  const page = await pageAs(member, `/code/${project.id}`);
  await openPreview(page);
  await expect(page.getByText('Код не вызвал ни одной функции api')).toBeVisible({ timeout: 2_500 });
  // Ожидание — на стороне теста: page.waitForTimeout идёт через страницу и с ней зависает.
  await new Promise((resolve) => setTimeout(resolve, 4_000));
  const started = Date.now();
  const responsive = await Promise.race([
    page.evaluate(() => document.title).then(() => true),
    new Promise<boolean>((resolve) => setTimeout(() => resolve(false), 5_000)),
  ]);
  test.info().annotations.push({
    type: 'frame-process',
    description: `${browserName}: вкладка портала ${responsive ? `отвечает (${Date.now() - started} мс) — фрейм в отдельном процессе` : 'не отвечает — фрейм в процессе портала'}`,
  });
  if (responsive) {
    await page.getByRole('button', { name: 'Запустить заново' }).first().click();
    await expect(page.locator('iframe[title="Предпросмотр: widget.html"]')).toBeAttached();
  }
});

test('предпросмотр выключен (code.preview.enabled: false): кнопки нет, слой образца — 403, веб-виды создаются и выгружаются @стенд', async ({
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const restore = restartApiWith('code:\n  build:\n    enabled: true\n  preview:\n    enabled: false\n');
  try {
    expect((await (await member.api.get('/api/config')).json()).code.preview.enabled).toBe(false);
    const project = await createWebProject(member.api, 'html_widget', 'Без предпросмотра');
    await expectError(await member.api.get(`/api/code/projects/${project.id}/preview-layer?schema=cadastre&table=parcels`), 403, 'feature_disabled');
    expect((await member.api.get(`/api/code/projects/${project.id}/export`)).status()).toBe(200);
    const page = await pageAs(member, `/code/${project.id}`);
    await expect(page.locator('.monaco-editor').first()).toBeVisible({ timeout: 20_000 });
    await expect(page.getByRole('button', { name: 'Предпросмотр', exact: true })).toHaveCount(0);
    await expect(page.locator('iframe[src*="code-preview"]')).toHaveCount(0);
  } finally {
    restore();
  }
});
