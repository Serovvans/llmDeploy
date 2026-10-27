/**
 * Подключения к базам заказчика на тестовой базе стенда `customer-db` (docs/portal-design.md §10,
 * критерии 42–45; docs/portal-api.md §7.6, §7.5.18, §13.1 — строка qa-engineer; CLAUDE.md,
 * пайплайн «Код», правила 20–22). `portal-api` перезапускается с `db.enabled: true` на время файла и
 * после него возвращается к накладке стенда. Пароль ролей тестовой базы — `CUSTOMER_DB_PASSWORD` из
 * окружения прогона.
 */
import type { Page } from '@playwright/test';

import { createDialog, expectError, parseStream, stubReply, type StreamEvent } from '../support/portal';
import {
  checkConnection,
  createConnection,
  customerPassword,
  DB_ON_YAML,
  expectReadOnlyTransactions,
  portalStatements,
  refreshSnapshot,
} from '../support/db';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { apiLog, customerSql, restartApiWithSections, standCompose } from '../support/stand';
import { saveFile } from '../support/code';
import { createWebProject, sqlCheck } from '../support/web';

let restore: (() => void) | null = null;

// Проект `stand` — один исполнитель: накладка включается один раз на исполнителя, падение теста не
// отменяет остальные (новый исполнитель включает её заново).
test.beforeAll(() => {
  restore = restartApiWithSections(DB_ON_YAML);
});

test.afterAll(() => {
  restore?.();
});

/** Строки базы, которые не должны оставаться нигде, кроме событий потока. */
const OWNER_NAME = 'Правообладатель 1';
const OWNER_PHONE = '+7 900 000-00-0';

interface Block {
  index: number;
  status: string;
  reason: string | null;
  db: { excluded_columns: { table: string; column: string; reason: string }[] } | null;
  fixture: { table: string; rows: (string | null)[][] | null; origin: string; invented_reason: string | null }[] | null;
  result: { rows: (string | null)[][] | null; row_count: number } | null;
  affected_rows: number | null;
}

function lastRun(events: StreamEvent[]): Block[] {
  return must(events.filter((event) => event.event === 'sql_run').at(-1), 'событие sql_run').data.blocks as Block[];
}

/** Вопрос SQL-помощнику с выбором подключения у диалога (§7.6.9). */
async function askWithSelection(member: Member, dialogId: string, content: string, testRun: boolean): Promise<StreamEvent[]> {
  const response = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
    data: { content, action: 'write', dialect: 'postgres', schema_id: null, test_run: testRun },
    timeout: 120_000,
  });
  expect(response.status(), await response.text()).toBe(200);
  return parseStream(await response.text());
}

async function selectTables(member: Member, dialogId: string, connectionId: string, tables: string[]): Promise<void> {
  const response = await member.api.patch(`/api/dialogs/${dialogId}`, {
    data: { db: { connection_id: connectionId, tables: tables.map((name) => ({ schema: 'cadastre', name })) } },
  });
  expect(response.status(), await response.text()).toBe(200);
}

/** Блок `sql` ответа заглушки. */
function sqlBlock(text: string): string {
  return `\`\`\`sql\n${text}\n\`\`\``;
}

/** Сколько строк выгрузки базы портала содержат `needle`: поиск идёт в контейнере, выгрузка велика. */
function portalDumpMatches(needle: string): number {
  const output = standCompose(
    'exec', '-T', '-e', `NEEDLE=${needle}`, 'portal-db', 'sh', '-c',
    'pg_dump -U portal --data-only portal | grep -cF -e "$NEEDLE" || true',
  );
  return Number(output.toString('utf8').trim());
}

test('права ролей тестовой базы: лишние права — отказ с перечнем, пароль не возвращается @стенд', async ({ adminApi, newMember }) => {
  test.setTimeout(180_000);
  const since = new Date().toISOString();
  const password = customerPassword();
  const bodies: string[] = [];
  const expected: Record<string, { status: string; kind?: string }> = {
    portal_reader: { status: 'read_only' },
    portal_writer: { status: 'excess', kind: 'write' },
    portal_owner: { status: 'excess', kind: 'owner' },
    portal_writeall: { status: 'excess', kind: 'predefined_role' },
    portal_super: { status: 'excess', kind: 'superuser' },
    portal_setrole: { status: 'excess', kind: 'write' },
    portal_definer: { status: 'read_only' },
  };
  const ids: Record<string, string> = {};
  for (const [role, want] of Object.entries(expected)) {
    const created = await createConnection(adminApi, role);
    bodies.push(JSON.stringify(created));
    expect(created.rights.status).toBe('unknown');
    ids[role] = created.id;
    const checked = await checkConnection(adminApi, created.id);
    bodies.push(JSON.stringify(checked));
    expect(checked.last_error, role).toBeNull();
    expect(checked.rights.status, `${role}: ${JSON.stringify(checked.rights)}`).toBe(want.status);
    if (want.kind) {
      expect(checked.rights.findings.map((finding: { kind: string }) => finding.kind), role).toContain(want.kind);
    } else {
      expect(checked.rights.findings, role).toEqual([]);
    }
  }
  const definer = await (await adminApi.get('/api/admin/db-connections')).json();
  bodies.push(JSON.stringify(definer));
  const definerRow = must(
    definer.items.find((item: { id: string }) => item.id === ids.portal_definer),
    'подключение portal_definer',
  );
  expect(definerRow.rights.warnings.map((warning: { kind: string }) => warning.kind)).toEqual(['definer_function']);
  expect(JSON.stringify(definerRow.rights.warnings)).toContain('cadastre.recalc_area');

  // Сотрудник: в списке usable отражает итог прав, маршруты администратора закрыты.
  const member = await newMember();
  const list = await (await member.api.get('/api/db/connections')).json();
  bodies.push(JSON.stringify(list));
  const byId = new Map(list.items.map((item: { id: string; usable: boolean }) => [item.id, item.usable]));
  expect(byId.get(ids.portal_reader)).toBe(true);
  expect(byId.get(ids.portal_writer)).toBe(false);
  expect(byId.get(ids.portal_definer)).toBe(true);
  bodies.push(JSON.stringify(await refreshSnapshot(member.api, must(ids.portal_reader, 'reader'))));
  await expectError(await member.api.post(`/api/db/connections/${ids.portal_writer}/snapshot`), 409, 'db_connection_unusable');
  for (const call of [
    () => member.api.get('/api/admin/db-connections'),
    () => member.api.post('/api/admin/db-connections', { data: { title: 'x' } }),
    () => member.api.patch(`/api/admin/db-connections/${ids.portal_reader}`, { data: { title: 'Захват' } }),
    () => member.api.post(`/api/admin/db-connections/${ids.portal_reader}/check`),
    () => member.api.delete(`/api/admin/db-connections/${ids.portal_reader}`),
  ]) {
    const response = await call();
    expect(response.status(), await response.text()).toBe(403);
  }

  // Пароль: ни в одном ответе, ни в базе портала открытым текстом, ни в журнале portal-api.
  for (const body of bodies) {
    expect(body).not.toContain(password);
  }
  expect(portalDumpMatches(password)).toBe(0);
  expect(apiLog(since)).not.toContain(password);
});

test('администратор добавляет подключение в интерфейсе, «Проверить подключение» показывает итог @стенд', async ({ newMember, pageAs }) => {
  test.setTimeout(120_000);
  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/databases');
  await expect(page.getByRole('heading', { level: 1, name: 'Управление' })).toBeVisible();
  await page.getByRole('button', { name: 'Добавить подключение' }).first().click();
  const form = page.getByRole('dialog');
  const title = `Кадастр UI ${Date.now()}`;
  await form.getByLabel('Название').fill(title);
  await form.getByLabel('Адрес').fill('customer-db');
  await form.getByLabel('База данных').fill('cadastre');
  await form.getByLabel('Учётная запись').fill('portal_writer');
  await form.getByRole('textbox', { name: /^Пароль/ }).fill(customerPassword());
  await form.locator('label').filter({ hasText: /^Без шифрования/ }).click();
  const schemas = form.getByLabel('Схемы базы');
  await schemas.click();
  await page.keyboard.type('cadastre');
  await page.keyboard.press('Enter');
  await form.locator('label').filter({ hasText: /^Всем сотрудникам/ }).click();
  await form.getByRole('button', { name: 'Сохранить и проверить' }).click();
  await expect(form.getByText('Подключение не используется: у учётной записи portal_writer лишние права')).toBeVisible({ timeout: 30_000 });
  await expect(form.getByText(/INSERT/).first()).toBeVisible();

  // В таблице — итог проверки; «Проверить подключение» из меню строки повторяет её.
  await page.keyboard.press('Escape');
  const row = page.getByRole('row').filter({ hasText: title });
  await expect(row.getByText(/Лишние права: \d+/)).toBeVisible();
  expect(await page.content()).not.toContain(customerPassword());
});

test('только чтение: запросы модели — в песочнице, в базу — только чтение портала в READ ONLY, строки не хранятся @стенд', async ({
  adminApi,
  newMember,
}) => {
  test.setTimeout(240_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader', {
    exclusions: [{ schema: 'cadastre', table: 'owners', column: 'phone' }],
  });
  const snapshot = await refreshSnapshot(member.api, connection.id);
  const leasesBefore = customerSql('SELECT count(*) FROM cadastre.leases');
  const statusesBefore = customerSql("SELECT count(*) FROM cadastre.parcels WHERE status = 'Изменено'");

  // Снимок: таблицы, ключи, комментарии, геометрия; исключённый столбец отмечен.
  const parcels = must(snapshot.tables.find((table: { name: string }) => table.name === 'parcels'), 'parcels в снимке');
  expect(parcels.comment).toBe('Земельные участки');
  const geom = must(parcels.columns.find((column: { name: string }) => column.name === 'geom'), 'geom');
  expect(geom.geometry).toEqual({ type: 'MultiPolygon', srid: 4326 });
  expect(parcels.columns.find((column: { name: string }) => column.name === 'id').primary_key, 'первичный ключ parcels.id в снимке').toBe(true);
  const leases = must(snapshot.tables.find((table: { name: string }) => table.name === 'leases'), 'leases в снимке');
  expect(leases.columns.find((column: { name: string }) => column.name === 'parcel_id').references).toEqual({ schema: 'cadastre', table: 'parcels', column: 'id' });
  const owners = must(snapshot.tables.find((table: { name: string }) => table.name === 'owners'), 'owners в снимке');
  expect(owners.columns.find((column: { name: string }) => column.name === 'phone').excluded).toBe(true);

  const dialogId = await createDialog(member.api, 'sql');
  const memoryBefore = (await (await member.api.get(`/api/dialogs/${dialogId}/memory?action=write&dialect=postgres`)).json()).limit_tokens;
  await selectTables(member, dialogId, connection.id, ['leases', 'parcels', 'owners']);
  const memoryAfter = (await (await member.api.get(`/api/dialogs/${dialogId}/memory?action=write&dialect=postgres`)).json()).limit_tokens;
  // Отмеченные таблицы входят в постоянную часть запроса: места под разговор становится меньше.
  expect(memoryAfter).toBeLessThan(memoryBefore);

  const since = new Date().toISOString();
  // Проверяется не больше трёх блоков ответа (sql.sandbox): два вопроса по три блока.
  const writes = [
    sqlBlock('DELETE FROM cadastre.leases;'),
    sqlBlock("UPDATE cadastre.parcels SET status = 'Изменено';"),
    sqlBlock('SELECT full_name, phone FROM cadastre.owners ORDER BY id LIMIT 3;'),
  ].join('\n\n');
  const reads = [
    sqlBlock('SELECT count(*) FROM cadastre.parcels;'),
    sqlBlock('SELECT * FROM cadastre.secret_registry;'),
    // Числовой столбец образца в песочнице — числом (§7.6.7, А-134), а не текстом.
    sqlBlock('SELECT id FROM cadastre.parcels WHERE area > 2000 ORDER BY id LIMIT 1;'),
  ].join('\n\n');
  const first = await askWithSelection(member, dialogId, stubReply(writes), true);
  expect(first.at(-1)?.event).toBe('done');
  const [deleted, updated, selected] = lastRun(first);
  const second = await askWithSelection(member, dialogId, stubReply(reads), true);
  const [counted, outside, typed] = lastRun(second);
  expect([deleted, updated, selected, counted].map((block) => block?.status), JSON.stringify([deleted, updated, selected, counted])).toEqual([
    'passed',
    'passed',
    'passed',
    'passed',
  ]);
  expect(typed?.status, `типы образца в песочнице: ${JSON.stringify(typed)}`).toBe('passed');
  expect(typed?.result?.rows, 'первый участок площадью больше 2000').toEqual([['27']]);
  expect(deleted?.affected_rows).toBe(20);
  expect(deleted?.fixture?.[0]?.origin).toBe('sample');
  expect(updated?.affected_rows).toBe(50);
  expect(selected?.result?.rows?.[0]).toEqual([OWNER_NAME, null]);
  expect(selected?.db?.excluded_columns).toEqual(
    expect.arrayContaining([
      { table: 'owners', column: 'phone', reason: 'excluded' },
      { table: 'owners', column: 'passport_scan', reason: 'binary' },
    ]),
  );
  expect(JSON.stringify(selected?.fixture)).not.toContain(OWNER_PHONE);
  // Образец — не больше db.sample_rows_check строк, хотя в таблице их 60.
  expect(counted?.result?.rows).toEqual([['50']]);
  expect(outside?.fixture?.find((table) => table.table.includes('secret_registry'))?.origin ?? 'invented').toBe('invented');

  // База заказчика не изменилась; в её журнале — только чтение портала в транзакциях READ ONLY.
  expect(customerSql('SELECT count(*) FROM cadastre.leases')).toEqual(leasesBefore);
  expect(customerSql("SELECT count(*) FROM cadastre.parcels WHERE status = 'Изменено'")).toEqual(statusesBefore);
  const statements = portalStatements(since);
  expect(statements.length).toBeGreaterThan(0);
  expectReadOnlyTransactions(statements);
  const all = statements.map((item) => item.text).join('\n');
  expect(all).not.toContain('DELETE FROM cadastre');
  expect(all).not.toContain("'Изменено'");
  expect(all).not.toContain('secret_registry');
  expect(all).toContain('"cadastre"."leases"');
  expect(new Set(statements.map((item) => item.role))).toEqual(new Set(['portal_reader']));

  // Строки базы — только в событиях: в истории, базе портала и журнале их нет.
  const messages = await (await member.api.get(`/api/dialogs/${dialogId}/messages`)).json();
  const saved = JSON.stringify(messages);
  expect(saved).not.toContain(OWNER_NAME);
  expect(saved).not.toContain(OWNER_PHONE);
  const answer = must((messages.items as { role: string; sql_run: { blocks: Block[] } | null }[]).filter((item) => item.role === 'assistant').at(-1), 'ответ');
  for (const block of answer.sql_run?.blocks ?? []) {
    expect(block.result?.rows ?? null).toBeNull();
    for (const table of block.fixture ?? []) {
      expect(table.rows).toBeNull();
    }
  }
  expect(portalDumpMatches(OWNER_NAME)).toBe(0);
  expect(apiLog(since)).not.toContain(OWNER_NAME);

  // Схема отмеченных таблиц уходит модели блоком в вопросе (заглушка повторяет начало вопроса).
  const echo = await askWithSelection(member, dialogId, 'Сколько договоров аренды?', false);
  const text = echo.filter((event) => event.event === 'delta').map((event) => event.data.text as string).join('');
  expect(text).toContain('<схема');
  expect(text).toMatch(/CREATE TABLE cadastre\.(leases|owners|parcels)/);
});

test('лишние права: после проверки к базе нет ни одного обращения — ни снимок, ни образец @стенд', async ({ adminApi, newMember }) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader');
  await refreshSnapshot(member.api, connection.id);
  const dialogId = await createDialog(member.api, 'sql');
  await selectTables(member, dialogId, connection.id, ['leases']);

  // Та же база под ролью с правом записи: смена учётной записи сбрасывает итог прав.
  const patched = await adminApi.patch(`/api/admin/db-connections/${connection.id}`, { data: { username: 'portal_writer' } });
  expect(patched.status(), await patched.text()).toBe(200);
  expect((await patched.json()).rights.status).toBe('unknown');
  const checkSince = new Date().toISOString();
  const checked = await checkConnection(adminApi, connection.id);
  expect(checked.rights.status).toBe('excess');
  // В проверке — только запросы прав: ни каталога для снимка, ни строк таблиц.
  const checkStatements = portalStatements(checkSince);
  expectReadOnlyTransactions(checkStatements);
  const checkText = checkStatements.map((item) => item.text).join('\n');
  expect(checkText).not.toContain('obj_description');
  expect(checkText).not.toContain('"cadastre"."');

  const since = new Date().toISOString();
  await expectError(await member.api.post(`/api/db/connections/${connection.id}/snapshot`), 409, 'db_connection_unusable');
  const events = await askWithSelection(member, dialogId, stubReply(sqlBlock('SELECT count(*) FROM cadastre.leases;')), true);
  const [block] = lastRun(events);
  expect(block?.status).toBe('unavailable');
  expect(block?.reason).toBe('connection_unusable');
  // Сохранённый снимок помощник видит по-прежнему (ПД §5.8).
  expect((await member.api.get(`/api/db/connections/${connection.id}/snapshot`)).status()).toBe(200);
  await new Promise((resolve) => setTimeout(resolve, 1_000));
  expect(portalStatements(since)).toEqual([]);
});

test('сотрудник без доступа: подключение, снимок и образец — «не найдено» @стенд', async ({ adminApi, newMember }) => {
  const allowed = await newMember();
  const outsider = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader', { userIds: [allowed.account.id] });
  await refreshSnapshot(allowed.api, connection.id);

  const list = await (await outsider.api.get('/api/db/connections')).json();
  expect(list.items.map((item: { id: string }) => item.id)).not.toContain(connection.id);
  expect((await outsider.api.get(`/api/db/connections/${connection.id}/snapshot`)).status()).toBe(404);
  expect((await outsider.api.post(`/api/db/connections/${connection.id}/snapshot`)).status()).toBe(404);
  const dialogId = await createDialog(outsider.api, 'sql');
  await expectError(
    await outsider.api.patch(`/api/dialogs/${dialogId}`, { data: { db: { connection_id: connection.id, tables: [] } } }),
    422,
    'db_connection_not_found',
  );
  const widget = await createWebProject(outsider.api, 'html_widget', 'Без доступа');
  await expectError(
    await outsider.api.patch(`/api/code/projects/${widget.id}`, { data: { db: { connection_id: connection.id, tables: [] } } }),
    422,
    'db_connection_not_found',
  );

  // Чужой проект с выбором подключения: слой образца и «Проверить запрос» — 404.
  const own = await createWebProject(allowed.api, 'html_widget', 'С доступом');
  const select = await allowed.api.patch(`/api/code/projects/${own.id}`, {
    data: { db: { connection_id: connection.id, tables: [{ schema: 'cadastre', name: 'parcels' }] } },
  });
  expect(select.status(), await select.text()).toBe(200);
  expect((await outsider.api.get(`/api/code/projects/${own.id}/preview-layer?schema=cadastre&table=parcels`)).status()).toBe(404);
  expect((await outsider.api.post(`/api/code/projects/${own.id}/sql-check`, { data: { sql: 'SELECT 1' } })).status()).toBe(404);

  // Закрыли доступ после выбора — выбор недоступен.
  const closed = await adminApi.patch(`/api/admin/db-connections/${connection.id}`, { data: { access: { all: false, user_ids: [outsider.account.id] } } });
  expect(closed.status(), await closed.text()).toBe(200);
  await expectError(await allowed.api.get(`/api/code/projects/${own.id}/preview-layer?schema=cadastre&table=parcels`), 409, 'db_connection_unavailable');
});

test('образец слоя предпросмотра: GeoJSON в WGS 84 без исключённых столбцов; «Проверить запрос» с параметром @стенд', async ({
  adminApi,
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader', {
    exclusions: [{ schema: 'cadastre', table: 'parcels', column: 'area' }],
  });
  await refreshSnapshot(member.api, connection.id);
  const plugin = await createWebProject(member.api, 'js_plugin', 'Слой из базы');
  const memoryBefore = (await (await member.api.get(`/api/dialogs/${plugin.dialog_id}/memory?action=write`)).json()).limit_tokens;
  const select = await member.api.patch(`/api/code/projects/${plugin.id}`, {
    data: { db: { connection_id: connection.id, tables: [{ schema: 'cadastre', name: 'parcels' }] } },
  });
  expect(select.status(), await select.text()).toBe(200);
  const memoryAfter = (await (await member.api.get(`/api/dialogs/${plugin.dialog_id}/memory?action=write`)).json()).limit_tokens;
  // Отмеченные таблицы входят в постоянную часть запроса: места под разговор становится меньше.
  expect(memoryAfter).toBeLessThan(memoryBefore);

  const since = new Date().toISOString();
  const layer = await member.api.get(`/api/code/projects/${plugin.id}/preview-layer?schema=cadastre&table=parcels`);
  expect(layer.status(), await layer.text()).toBe(200);
  const body = await layer.json();
  expect(body.rows).toBe(50);
  expect(body.excluded_columns).toBe(1);
  expect(body.geometry).toEqual({ column: 'geom', type: 'MultiPolygon' });
  expect(body.features.features).toHaveLength(50);
  for (const feature of body.features.features) {
    expect(Object.keys(feature.properties).sort()).toEqual(['cadastral_number', 'id', 'status']);
    for (const [lon, lat] of feature.geometry.coordinates[0][0]) {
      expect(lon).toBeGreaterThan(55);
      expect(lon).toBeLessThan(56);
      expect(lat).toBeGreaterThan(51);
      expect(lat).toBeLessThan(52);
    }
  }
  expect(layer.headers()['cache-control']).toContain('no-store');
  expectReadOnlyTransactions(portalStatements(since));
  // Таблица вне отмеченных — ошибка поля, к базе не идёт.
  const other = await member.api.get(`/api/code/projects/${plugin.id}/preview-layer?schema=cadastre&table=owners`);
  expect(other.status()).toBe(422);

  // «Проверить запрос» с параметром: подстановка значения, образец из базы.
  const checked = await sqlCheck(member.api, plugin.id, 'SELECT cadastral_number FROM cadastre.parcels WHERE id = @id', [{ name: '@id', value: '5' }]);
  const [block] = lastRun(checked);
  expect(block?.status, JSON.stringify(block)).toBe('passed');
  expect(block?.result?.rows).toEqual([['56:44:0501:105']]);

  // В интерфейсе: слой из образца в предпросмотре плагина.
  const page: Page = await pageAs(member, `/code/${plugin.id}`);
  await page.getByRole('button', { name: 'Предпросмотр', exact: true }).first().click();
  await page.getByRole('button', { name: 'Данные слоя:' }).click();
  await page.getByText('cadastre.parcels', { exact: true }).last().click();
  await expect(page.getByText(/Слой: cadastre\.parcels, 50 объектов · геометрия в WGS 84 · без 1 столбца/)).toBeVisible({ timeout: 20_000 });
  await page.frameLocator('iframe[title="Предпросмотр: plugin.js"]').getByRole('button', { name: 'Запустить плагин' }).click();
  await expect(page.getByText('→ слой предпросмотра: cadastre.parcels, 50 объектов').first()).toBeVisible();

  // «Проверить запрос» из редактора: выделенный запрос, значение параметра, итог на образце.
  const widget = await createWebProject(member.api, 'html_widget', 'Запрос из редактора');
  const chosen = await member.api.patch(`/api/code/projects/${widget.id}`, {
    data: { db: { connection_id: connection.id, tables: [{ schema: 'cadastre', name: 'parcels' }] } },
  });
  expect(chosen.status(), await chosen.text()).toBe(200);
  await saveFile(member.api, widget.id, must(widget.files[0], 'файл'), 'SELECT cadastral_number FROM cadastre.parcels WHERE id = @id');
  const editorPage = await pageAs(member, `/code/${widget.id}`);
  const editor = editorPage.locator('.monaco-editor').first();
  await expect(editor).toBeVisible({ timeout: 20_000 });
  await editor.click();
  await editorPage.keyboard.press('ControlOrMeta+A');
  await editor.click({ button: 'right', position: { x: 120, y: 10 } });
  // Monaco ignores mouseup on a menu item for 100 ms after the menu renders, so an immediate click
  // is sometimes lost; hover focuses the item and Enter runs it without that delay.
  await editorPage.getByRole('menuitem', { name: 'Проверить запрос' }).hover();
  await editorPage.keyboard.press('Enter');
  const modal = editorPage.getByRole('dialog');
  await modal.getByLabel('Значение параметра @id').fill('5');
  await modal.getByRole('button', { name: 'Проверить', exact: true }).click();
  await expect(editorPage.getByText('56:44:0501:105').first()).toBeVisible({ timeout: 30_000 });
});

test('предел времени обрывает обращение к базе @стенд', async ({ adminApi, newMember }) => {
  test.setTimeout(120_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader');
  await refreshSnapshot(member.api, connection.id);
  const dialogId = await createDialog(member.api, 'sql');
  const response = await member.api.patch(`/api/dialogs/${dialogId}`, {
    data: { db: { connection_id: connection.id, tables: [{ schema: 'cadastre', name: 'slow_view' }] } },
  });
  expect(response.status(), await response.text()).toBe(200);
  const started = Date.now();
  const events = await askWithSelection(member, dialogId, stubReply(sqlBlock('SELECT id FROM cadastre.slow_view;')), true);
  const [block] = lastRun(events);
  expect(block?.status, JSON.stringify(block)).toBe('unavailable');
  expect(block?.reason).toBe('db_unavailable');
  expect(Date.now() - started).toBeLessThan(28_000);
  // Сеанс портала закрыт: в базе нет его соединений.
  await expect.poll(() => customerSql("SELECT count(*) FROM pg_stat_activity WHERE application_name = 'scancontrol-portal'")[0]).toBe('0');
});

test('образцы не разрешены: данные для проверки придумывает модель, к таблицам базы запроса нет @стенд', async ({ adminApi, newMember }) => {
  test.setTimeout(120_000);
  const member = await newMember();
  const connection = await createConnection(adminApi, 'portal_reader', { samples: false });
  await refreshSnapshot(member.api, connection.id);
  const dialogId = await createDialog(member.api, 'sql');
  await selectTables(member, dialogId, connection.id, ['parcels']);
  const since = new Date().toISOString();
  const events = await askWithSelection(member, dialogId, stubReply(sqlBlock('SELECT count(*) FROM cadastre.parcels;')), true);
  const [block] = lastRun(events);
  // Данные просили у модели: схема подключения — из снимка, пометку с ответом заглушки на стенде в неё не
  // положить, поэтому заглушка отвечает пустым набором, и блок — «тестовые данные не подготовлены».
  expect(block?.status, JSON.stringify(block)).toBe('not_checked');
  expect(block?.reason).toBe('test_data_failed');
  expect(block?.db).not.toBeNull();
  expect(portalStatements(since).map((item) => item.text).join('\n')).not.toContain('"cadastre"."parcels"');
});
