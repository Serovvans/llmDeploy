/**
 * Виды JS и HTML раздела «Код» (docs/portal-design.md §10, критерии 39 и 41; docs/portal-api.md
 * §7.5.16; docs/portal-ui.md §5.13.17–5.13.22): четыре вида из шаблонов, подсказки редактора по
 * `api`, подчёркивание синтаксической ошибки, выгрузка с памяткой, отличия веб-видов от GPTool,
 * правка помощника и версия.
 */
import { addFile, askCode, downloadZip, editOf, fileOf, getProject, lastAnswerId } from '../support/code';
import { expect, test } from '../support/fixtures';
import { expectError } from '../support/portal';
import { createWebProject, TEMPLATE_FILE, type WebKind, type WebProject } from '../support/web';

const KINDS: { kind: WebKind; button: string; modal: string }[] = [
  { kind: 'js_plugin', button: 'Создать JS-плагин виджета', modal: 'Новый JS-плагин виджета' },
  { kind: 'html_widget', button: 'Создать HTML-виджет', modal: 'Новый HTML-виджет' },
  { kind: 'catalog', button: 'Создать кастомизацию Каталога карт', modal: 'Новая кастомизация Каталога карт' },
  { kind: 'card_hooks', button: 'Создать хуки карточки объекта', modal: 'Новые хуки карточки объекта' },
];

test('четыре вида проектов создаются из шаблонов в интерфейсе', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member, '/code');
  for (const { kind, button, modal } of KINDS) {
    await page.goto('/code');
    await page.getByRole('button', { name: button }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText(modal)).toBeVisible();
    // Имя веб-вида — свободный текст (А-126): пробелы, кириллица, знаки.
    const name = `Проект «${kind}» №1`;
    await dialog.getByLabel('Название проекта').fill(name);
    await dialog.getByRole('button', { name: 'Создать проект' }).click();
    await expect(page).toHaveURL(/\/code\/[0-9a-f-]{36}$/);
    const id = /\/code\/([0-9a-f-]{36})$/.exec(page.url())?.[1] ?? '';
    const project = (await getProject(member.api, id)) as WebProject;
    expect(project.kind).toBe(kind);
    expect(project.name).toBe(name);
    expect(project.files.map((file) => file.path)).toEqual([TEMPLATE_FILE[kind]]);
    expect(project.files[0]?.content.length).toBeGreaterThan(50);
    await expect(page.getByRole('heading', { level: 1, name })).toBeVisible();
    await expect(page.getByText(TEMPLATE_FILE[kind], { exact: true }).first()).toBeVisible();
  }
});

test('подсказки редактора знают функции api, синтаксическая ошибка подчёркивается', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'js_plugin', 'Подсказки api');
  const page = await pageAs(member, `/code/${project.id}`);
  const editor = page.locator('.monaco-editor').first();
  await expect(editor).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('.monaco-editor .squiggly-error')).toHaveCount(0, { timeout: 15_000 });

  await editor.click();
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.type('api.createP');
  await page.keyboard.press('Control+Space');
  const suggest = page.locator('.monaco-editor .suggest-widget');
  await expect(suggest).toBeVisible({ timeout: 15_000 });
  await expect(suggest.getByText('createPoint').first()).toBeVisible();

  await page.keyboard.press('Escape');
  await page.keyboard.press('ControlOrMeta+A');
  await page.keyboard.type('function broken( {');
  await expect(page.locator('.monaco-editor .squiggly-error').first()).toBeAttached({ timeout: 15_000 });
  await expect(page.getByRole('button', { name: /^Синтаксис: \d+ ошиб/ })).toBeVisible();
});

test('выгрузка веб-видов: файлы проекта и памятка с местом вставки и настройками CoGIS', async ({ newMember }) => {
  const member = await newMember();
  for (const { kind } of KINDS) {
    const project = await createWebProject(member.api, kind, `Выгрузка ${kind}`);
    if (kind === 'catalog') {
      const mobile = await addFile(member.api, project.id, 'mobile.js', 'console.log(1);');
      const patched = await member.api.patch(`/api/code/projects/${project.id}`, {
        data: { catalog_files: [{ file_id: mobile.id, insertion: 'body_start', clients: ['smartphone'] }] },
      });
      expect(patched.status(), await patched.text()).toBe(200);
    }
    const { response, files } = await downloadZip(member.api, `/api/code/projects/${project.id}/export`);
    expect(response.headers()['content-disposition']).toContain(`filename*=UTF-8''${encodeURIComponent(`Выгрузка ${kind}`)}.zip`);
    const template = TEMPLATE_FILE[kind];
    expect(files.get(template)?.toString('utf8')).toBe(project.files[0]?.content);
    const memoBytes = files.get('Памятка.txt');
    expect(memoBytes, 'Памятка.txt в архиве').toBeDefined();
    expect([...(memoBytes ?? Buffer.alloc(0)).subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    const memo = (memoBytes ?? Buffer.alloc(0)).toString('utf8');
    expect(memo).toContain(`${template} — код`);
    expect(memo).toContain('Памятка.txt — эта памятка');
    expect(memo).not.toMatch(/\{(files|main_file|customizations)\}/);
    if (kind === 'catalog') {
      expect(files.has('mobile.js')).toBe(true);
      expect(memo).toContain('HTML-кастомизация');
      expect(memo).toContain('конец <head>');
      expect(memo).toContain('начало <body>');
      expect(memo).toContain('смартфон');
    } else if (kind === 'card_hooks') {
      expect(memo).toContain('\\Customer\\openProject\\');
      expect(memo).toContain(`Файл ${template}`);
    } else {
      expect(memo).toContain(`всё содержимое файла ${template}`);
    }
  }
});

test('у веб-видов нет второго файла, сборки, архива, пакетов и исправления сборкой', async ({ newMember }) => {
  const member = await newMember();
  const widget = await createWebProject(member.api, 'html_widget', 'Ограничения вида');
  const base = `/api/code/projects/${widget.id}`;
  await expectError(await member.api.post(`${base}/files`, { data: { path: 'second.html', content: '<p></p>' } }), 409, 'single_file_project');
  await expectError(await member.api.delete(`${base}/files/${widget.files[0]?.id}`), 409, 'single_file_project');
  await expectError(await member.api.post(`${base}/build`), 409, 'wrong_project_kind');
  await expectError(await member.api.get(`${base}/build/archive`), 409, 'wrong_project_kind');
  await expectError(await member.api.patch(base, { data: { packages: ['Newtonsoft.Json'] } }), 422, 'validation_error');
  const fix = await member.api.post(`/api/dialogs/${widget.dialog_id}/messages`, {
    data: { content: 'Исправь', action: 'fix', file_ids: null, target: null },
  });
  await expectError(fix, 422, 'validation_error');

  const catalog = await createWebProject(member.api, 'catalog', 'Расширения Каталога');
  const wrong = await member.api.post(`/api/code/projects/${catalog.id}/files`, { data: { path: 'Tool.cs', content: '//' } });
  expect(wrong.status()).toBe(422);
  expect(JSON.stringify(await wrong.json())).toContain('extension_not_allowed');
});

test('правка помощника веб-вида меняет файл только после «Принять» и создаёт версию', async ({ newMember }) => {
  const member = await newMember();
  const project = await createWebProject(member.api, 'js_plugin', 'Правка плагина');
  const before = project.files[0]?.content ?? '';
  const events = await askCode(member.api, project.dialog_id, 'Добавь комментарий');
  const edit = editOf(events);
  expect(edit.files.map((file) => file.path)).toEqual(['plugin.js']);
  expect(edit.verification).toBeNull();
  expect(fileOf(await getProject(member.api, project.id), 'plugin.js').content).toBe(before);

  const messageId = await lastAnswerId(member.api, project.dialog_id);
  const decision = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
    data: { accept: [{ path: 'plugin.js', revision: project.files[0]?.revision }] },
  });
  expect(decision.status(), await decision.text()).toBe(200);
  expect(fileOf(await getProject(member.api, project.id), 'plugin.js').content).toContain('// Правка заглушки.');
  const snapshots = (await (await member.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items;
  expect(snapshots.length).toBe(1);
});
