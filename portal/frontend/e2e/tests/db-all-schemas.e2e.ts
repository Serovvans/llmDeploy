/**
 * «Все схемы» у подключения к базе заказчика (docs/portal-design.md §10, критерий 46; docs/portal-api.md
 * §13.1 «Обязательные тесты 1.0.1» — строка qa-engineer, §13.5; docs/portal-ui.md §5.7, §5.14, Д-77).
 * Стенд `customer-db`: схемы `public`, `archive` (тёзки `parcels` и `notes`), `cadastre`; `closed` — без
 * USAGE у ролей входа. «Не все» проверяется подменой ответов API, предел стенда не меняется.
 */
import type { Page, Route } from '@playwright/test';

import { createDialog, parseStream, stubReply } from '../support/portal';
import { checkConnection, createConnection, DB_ON_YAML, portalStatements, refreshSnapshot } from '../support/db';
import { expect, test, type Member } from '../support/fixtures';
import { restartApiWithSections } from '../support/stand';

let restore: (() => void) | null = null;

test.beforeAll(() => {
  restore = restartApiWithSections(DB_ON_YAML);
});

test.afterAll(() => {
  restore?.();
});

const ALL_SCHEMAS_NOTE = 'Все схемы, которые учётной записи разрешено читать';

/** Открывает выбор таблиц SQL-помощника у нового диалога с подключением `connectionId`. */
async function openPicker(member: Member, connectionId: string, pageAs: (member: Member, path?: string) => Promise<Page>): Promise<Page> {
  const dialogId = await createDialog(member.api, 'sql');
  const response = await member.api.patch(`/api/dialogs/${dialogId}`, { data: { db: { connection_id: connectionId, tables: [] } } });
  expect(response.status(), await response.text()).toBe(200);
  const page = await pageAs(member, `/sql/${dialogId}`);
  await page.getByRole('button', { name: 'Выбрать таблицы' }).click();
  await expect(page.getByRole('dialog').getByText(/Таблицы для помощника/)).toBeVisible();
  return page;
}

/** Пункт меню Kebab (у `MenuItem` Контура нет роли `menuitem`). */
function menuItem(page: Page, name: string) {
  return page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: new RegExp(`^${name}$`) });
}

/** Схемы групп выбора таблиц в порядке показа. */
async function pickerSchemas(page: Page): Promise<string[]> {
  const legends = page.locator('fieldset > legend');
  await expect(legends.first()).toBeVisible();
  return (await legends.allTextContents()).map((text) => text.trim());
}

/** Блоки проверки последнего ответа SQL-помощника: вопрос с `test_run`, текст ответа — `sql`. */
async function testRun(member: Member, connectionId: string, sql: string): Promise<{ status: string; fixture: { table: string; origin: string }[] | null }> {
  const dialogId = await createDialog(member.api, 'sql');
  const patched = await member.api.patch(`/api/dialogs/${dialogId}`, { data: { db: { connection_id: connectionId, tables: [] } } });
  expect(patched.status(), await patched.text()).toBe(200);
  const response = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
    data: { content: stubReply(`\`\`\`sql\n${sql}\n\`\`\``), action: 'write', dialect: 'postgres', schema_id: null, test_run: true },
    timeout: 120_000,
  });
  expect(response.status(), await response.text()).toBe(200);
  const events = parseStream(await response.text());
  const run = events.filter((event) => event.event === 'sql_run').at(-1);
  const block = (run?.data.blocks as { status: string; fixture: { table: string; origin: string }[] | null }[] | undefined)?.[0];
  expect(block, JSON.stringify(events.map((event) => event.event))).toBeDefined();
  return block as { status: string; fixture: { table: string; origin: string }[] | null };
}

/** Таблицы, из которых портал читал образец (журнал `customer-db`), — `"схема"."таблица"`. */
function sampledTables(since: string): string[] {
  const found = new Set<string>();
  for (const { text } of portalStatements(since)) {
    for (const match of text.matchAll(/FROM\s+("[^"]+"\."[^"]+")/g)) {
      found.add(match[1] ?? '');
    }
  }
  return [...found];
}

test('все схемы: «Обновить схему» сразу после переключения, группы public → archive → cadastre, без closed и PostGIS @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const admin = await newMember('admin');
  const member = await newMember();
  // Подключение 1.0 со списком: схема загружена — следующее обновление рано (refresh_available_at в будущем).
  const connection = await createConnection(adminApi, 'portal_reader');
  await checkConnection(adminApi, connection.id);
  await refreshSnapshot(member.api, connection.id);

  const page = await pageAs(admin, '/admin/databases');
  const row = page.getByRole('row').filter({ hasText: connection.title });
  await expect(row).toBeVisible();
  await expect(row.getByText('все схемы', { exact: true })).toHaveCount(0);
  await row.getByRole('button', { name: `Действия: ${connection.title}` }).click();
  await menuItem(page, 'Изменить').click();
  const form = page.getByRole('dialog');
  await expect(form.getByRole('radio', { name: /^Только перечисленные/ })).toBeChecked();
  // Поле `input` у Radio Контура скрыто: щелчок — по подписи.
  await form.locator('label').filter({ hasText: /^Все схемы/ }).click();
  await expect(form.getByRole('radio', { name: /^Все схемы/ })).toBeChecked();
  await expect(form.getByText(ALL_SCHEMAS_NOTE)).toBeVisible();
  await expect(form.getByLabel('Схемы базы', { exact: true })).toHaveCount(0);
  await form.getByRole('button', { name: /^Сохранить/ }).click();
  await expect(form.getByText('Подключение работает, лишних прав нет')).toBeVisible({ timeout: 30_000 });
  await page.keyboard.press('Escape');

  await expect(row.getByText('все схемы', { exact: true })).toBeVisible();
  // Смена режима — параметр входа: обновить схему можно сразу, без «Обновить снова можно с…».
  await row.getByRole('button', { name: `Действия: ${connection.title}` }).click();
  await menuItem(page, 'Обновить схему').click();
  await expect(page.getByText(/^Схема обновлена: \d+ таблиц[аы]?$/)).toBeVisible({ timeout: 30_000 });

  const listed = await (await adminApi.get('/api/admin/db-connections')).json();
  const saved = listed.items.find((item: { id: string }) => item.id === connection.id);
  expect(saved.all_schemas).toBe(true);
  expect(saved.schemas).toEqual([]);

  const picker = await openPicker(member, connection.id, pageAs);
  expect(await pickerSchemas(picker)).toEqual(['public', 'archive', 'cadastre']);
  await expect(picker.getByText('spatial_ref_sys')).toHaveCount(0);
  await expect(picker.getByText('geometry_columns')).toHaveCount(0);
  await expect(picker.getByText('salaries')).toHaveCount(0);
  await expect(picker.getByText(/не все/)).toHaveCount(0);
});

test('таблица без схемы на образце: при «всех» parcels — из archive, notes — из public; со списком — как в 1.0 @стенд', async ({
  adminApi,
  newMember,
}) => {
  test.setTimeout(240_000);
  const member = await newMember();
  const all = await createConnection(adminApi, 'portal_reader');
  const patched = await adminApi.patch(`/api/admin/db-connections/${all.id}`, { data: { all_schemas: true } });
  expect(patched.status(), await patched.text()).toBe(200);
  await checkConnection(adminApi, all.id);
  const allSnapshot = await refreshSnapshot(member.api, all.id);
  const allNames = allSnapshot.tables.map((table: { schema: string; name: string }) => `${table.schema}.${table.name}`);
  expect([...new Set(allSnapshot.tables.map((table: { schema: string }) => table.schema))]).toEqual(['public', 'archive', 'cadastre']);
  expect(allNames).not.toContain('public.spatial_ref_sys');
  expect(allNames.some((name: string) => name.startsWith('closed.'))).toBe(false);

  let since = new Date().toISOString();
  const parcels = await testRun(member, all.id, 'SELECT count(*) FROM parcels;');
  expect(parcels.status, JSON.stringify(parcels)).toBe('passed');
  expect(parcels.fixture?.map((item) => [item.table, item.origin])).toEqual([['parcels', 'sample']]);
  expect(sampledTables(since)).toEqual(['"archive"."parcels"']);

  since = new Date().toISOString();
  const notes = await testRun(member, all.id, 'SELECT count(*) FROM notes;');
  expect(notes.status, JSON.stringify(notes)).toBe('passed');
  expect(notes.fixture?.map((item) => [item.table, item.origin])).toEqual([['notes', 'sample']]);
  expect(sampledTables(since)).toEqual(['"public"."notes"']);

  // Список `cadastre, public` — как в 1.0: порядок списка, PostGIS public входит, archive — нет.
  const list = await createConnection(adminApi, 'portal_reader');
  await checkConnection(adminApi, list.id);
  const listSnapshot = await refreshSnapshot(member.api, list.id);
  const listNames = listSnapshot.tables.map((table: { schema: string; name: string }) => `${table.schema}.${table.name}`);
  expect([...new Set(listSnapshot.tables.map((table: { schema: string }) => table.schema))]).toEqual(['cadastre', 'public']);
  expect(listNames).toContain('public.spatial_ref_sys');
  expect(listNames.some((name: string) => name.startsWith('archive.'))).toBe(false);
  since = new Date().toISOString();
  const listParcels = await testRun(member, list.id, 'SELECT count(*) FROM parcels;');
  expect(listParcels.fixture?.map((item) => [item.table, item.origin])).toEqual([['parcels', 'sample']]);
  expect(sampledTables(since)).toEqual(['"cadastre"."parcels"']);
});

/** Подменяет ответ `pattern` (метод `method`): `edit` правит JSON настоящего ответа. */
async function patchJson(page: Page, pattern: RegExp, method: string, edit: (body: any) => void): Promise<void> { // eslint-disable-line @typescript-eslint/no-explicit-any
  await page.route(pattern, async (route: Route) => {
    if (route.request().method() !== method) {
      await route.fallback();
      return;
    }
    const response = await route.fetch();
    const body = await response.json();
    edit(body);
    await route.fulfill({ response, json: body });
  });
}

test('схема загружена не целиком: «не все» в списке подключений, в «Схема обновлена…» и в выборе таблиц @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const admin = await newMember('admin');
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader');
  const patched = await adminApi.patch(`/api/admin/db-connections/${connection.id}`, { data: { all_schemas: true } });
  expect(patched.status(), await patched.text()).toBe(200);
  await checkConnection(adminApi, connection.id);
  const snapshot = await refreshSnapshot(member.api, connection.id);
  const count = snapshot.tables.length;
  expect(snapshot.truncated).toBe(false);

  // Администратор: список подключений и сообщение после «Обновить схему».
  const page = await pageAs(admin);
  await patchJson(page, /\/api\/admin\/db-connections$/, 'GET', (body) => {
    for (const item of body.items) {
      if (item.id === connection.id && item.snapshot) item.snapshot.truncated = true;
    }
  });
  // Схему только что загрузили — настоящее повторное обновление было бы «рано»: ответ подменяется целиком.
  await page.route(new RegExp(`/api/db/connections/${connection.id}/snapshot$`), async (route: Route) => {
    if (route.request().method() !== 'POST') {
      await route.fallback();
      return;
    }
    await route.fulfill({ json: { ...snapshot, truncated: true } });
  });
  await page.goto('/admin/databases');
  const row = page.getByRole('row').filter({ hasText: connection.title });
  const mark = row.getByText(new RegExp(`^${count} таблиц[аы]? — не все$`));
  await expect(mark).toBeVisible();
  await mark.hover();
  await expect(page.getByText(/перечислите их схемы у подключения или попросите администратора базы сузить права/).first()).toBeVisible();
  await row.getByRole('button', { name: `Действия: ${connection.title}` }).click();
  await menuItem(page, 'Обновить схему').click();
  await expect(page.getByText(new RegExp(`^Схема обновлена: ${count} таблиц[аы]? — не все$`))).toBeVisible({ timeout: 30_000 });

  // Сотрудник: строка происхождения схемы и пояснение в выборе таблиц.
  const dialogId = await createDialog(member.api, 'sql');
  const set = await member.api.patch(`/api/dialogs/${dialogId}`, { data: { db: { connection_id: connection.id, tables: [] } } });
  expect(set.status(), await set.text()).toBe(200);
  const memberPage = await pageAs(member);
  await patchJson(memberPage, /\/api\/db\/connections$/, 'GET', (body) => {
    for (const item of body.items) {
      if (item.id === connection.id && item.snapshot) item.snapshot.truncated = true;
    }
  });
  await patchJson(memberPage, new RegExp(`/api/db/connections/${connection.id}/snapshot$`), 'GET', (body) => {
    body.truncated = true;
  });
  await memberPage.goto(`/sql/${dialogId}`);
  await memberPage.getByRole('button', { name: 'Выбрать таблицы' }).click();
  await expect(memberPage.getByText(new RegExp(`^Схема загружена .+ · ${count} таблиц[аы]? — не все$`))).toBeVisible();
  await expect(
    memberPage.getByText('Портал загружает в схему не все таблицы такой большой базы. Нет нужной таблицы — сообщите администратору портала'),
  ).toBeVisible();
});
