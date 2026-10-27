/**
 * Изоляция маршрутов 1.0 раздела «Код» и выключенный флаг подключений (docs/portal-design.md §10,
 * критерии 37, 42, 44; docs/portal-api.md §7.5.16–7.5.18, §7.6.10; CLAUDE.md, пайплайн «Код»,
 * правило 5). На стенде по умолчанию `db.enabled: false` — как после релиза на ВМ.
 */
import type { APIRequestContext, APIResponse } from '@playwright/test';

import { newApi } from '../support/accounts';
import { addFile, getProject } from '../support/code';
import { expect, test } from '../support/fixtures';
import { expectError, saveSqlSchema } from '../support/portal';
import { createWebProject, sqlCheck } from '../support/web';

type Call = (api: APIRequestContext) => Promise<APIResponse>;

test('чужой проект веб-вида: файлы, настройки Каталога, выгрузка, «Проверить запрос» — «не найдено»', async ({ newMember }) => {
  const owner = await newMember();
  const intruder = await newMember();
  const project = await createWebProject(owner.api, 'catalog', 'Чужой каталог');
  const extra = await addFile(owner.api, project.id, 'extra.js', 'console.log(1);');
  const schemaId = await saveSqlSchema(owner.api, 'Схема владельца', 'CREATE TABLE t (id int);');
  expect((await owner.api.patch(`/api/code/projects/${project.id}`, { data: { schema_id: schemaId } })).status()).toBe(200);
  const own = await createWebProject(intruder.api, 'catalog', 'Свой каталог');
  const base = `/api/code/projects/${project.id}`;
  const calls: Record<string, Call> = {
    'проект': (api) => api.get(base),
    'настройки Каталога': (api) =>
      api.patch(base, { data: { catalog_files: [{ file_id: extra.id, insertion: 'head_start', clients: ['desktop'] }] } }),
    'файл: изменение': (api) => api.patch(`${base}/files/${extra.id}`, { data: { revision: extra.revision, content: '//' } }),
    'выгрузка': (api) => api.get(`${base}/export`),
    'проверить запрос': (api) => api.post(`${base}/sql-check`, { data: { sql: 'SELECT 1', parameters: [] } }),
    'свой проект, чужой файл Каталога': (api) =>
      api.patch(`/api/code/projects/${own.id}`, { data: { catalog_files: [{ file_id: extra.id, insertion: 'head_start', clients: ['desktop'] }] } }),
  };
  for (const [name, call] of Object.entries(calls)) {
    const response = await call(intruder.api);
    const status = response.status();
    // Чужой файл в своём проекте — ошибка поля, как у несуществующего (§7.5.16), остальное — 404.
    if (name.startsWith('свой проект')) {
      expect(status, name).toBe(422);
      expect(JSON.stringify(await response.json()), name).toContain('unknown_value');
    } else {
      expect(status, `${name}: ${await response.text()}`).toBe(404);
    }
  }
  const now = await getProject(owner.api, project.id);
  expect(now.files.map((file) => [file.path, file.revision])).toEqual(project.files.map((file) => [file.path, file.revision]).concat([['extra.js', 1]]));
});

test('без сессии маршруты 1.0 закрыты', async ({ newMember }) => {
  const owner = await newMember();
  const project = await createWebProject(owner.api, 'js_plugin', 'Без сессии');
  const anonymous = await newApi();
  const calls: Record<string, Call> = {
    'описание api': (api) => api.get('/api/code/web'),
    'библиотека клиента': (api) => api.get('/api/code/web/libraries/jquery.js'),
    'создание из шаблона': (api) => api.post('/api/code/projects', { data: { source: 'template', kind: 'js_plugin', name: 'X' } }),
    'проверить запрос': (api) => api.post(`/api/code/projects/${project.id}/sql-check`, { data: { sql: 'SELECT 1' } }),
    'слой предпросмотра': (api) => api.get(`/api/code/projects/${project.id}/preview-layer?schema=a&table=b`),
    'подключения сотрудника': (api) => api.get('/api/db/connections'),
    'подключения администратора': (api) => api.get('/api/admin/db-connections'),
  };
  for (const [name, call] of Object.entries(calls)) {
    const response = await call(anonymous);
    expect(response.status(), `${name}: ${await response.text()}`).toBe(401);
  }
  await anonymous.dispose();
});

test('описание api и библиотеки клиента CoGIS стенда отдаются сотруднику текстом', async ({ newMember }) => {
  const member = await newMember();
  const web = await (await member.api.get('/api/code/web')).json();
  expect(web.api_functions).toEqual(expect.arrayContaining(['createPoint', 'getLayer', 'searchInLayer', 'showAlert']));
  expect(web.api_types).toContain('declare const api: CogisApi;');
  expect(web.libraries).toEqual([
    { id: 'jquery', version: '3.7.1', available: true },
    { id: 'knockout', version: '3.5.1', available: true },
    { id: 'leaflet', version: '1.9.4', available: true },
  ]);
  const library = await member.api.get('/api/code/web/libraries/jquery.js');
  expect(library.status()).toBe(200);
  expect(library.headers()['content-type']).toContain('text/plain');
  expect(library.headers()['x-content-type-options']).toBe('nosniff');
  expect((await member.api.get('/api/code/web/libraries/evil.js')).status()).toBe(404);
});

test('подключения выключены (db.enabled: false): маршруты — 403, выбора нет, «Проверить запрос» работает по схеме', async ({ newMember, adminApi }) => {
  const member = await newMember();
  const config = await (await member.api.get('/api/config')).json();
  expect(config.db.enabled).toBe(false);

  await expectError(await adminApi.get('/api/admin/db-connections'), 403, 'feature_disabled');
  await expectError(await adminApi.post('/api/admin/db-connections', { data: {} }), 403, 'feature_disabled');
  await expectError(await member.api.get('/api/db/connections'), 403, 'feature_disabled');

  const widget = await createWebProject(member.api, 'html_widget', 'Без подключений');
  expect((widget as { db?: unknown }).db ?? null).toBeNull();
  await expectError(await member.api.get(`/api/code/projects/${widget.id}/preview-layer?schema=cadastre&table=parcels`), 403, 'feature_disabled');
  await expectError(
    await member.api.patch(`/api/code/projects/${widget.id}`, { data: { db: { connection_id: widget.id, tables: [] } } }),
    403,
    'feature_disabled',
  );

  // Без схемы — просьба выбрать схему; со схемой «Мои схемы» — проверка на придуманных данных.
  await expectError(
    await member.api.post(`/api/code/projects/${widget.id}/sql-check`, { data: { sql: 'SELECT 1' } }),
    422,
    'schema_required',
  );
  const table = 'CREATE TABLE parcels (id int PRIMARY KEY, status text)';
  const rows = "INSERT INTO parcels VALUES (1, 'Аренда'), (2, 'Свободен')";
  // Ответ заглушки на запрос тестовых данных — пометкой в схеме (docs/portal-api.md §12.2).
  const schemaText = `${table};\n-- [[stub:sql-data]]${JSON.stringify({ statements: [table, rows] })}[[/stub:sql-data]]`;
  const schemaId = await saveSqlSchema(member.api, 'Участки', schemaText);
  expect((await member.api.patch(`/api/code/projects/${widget.id}`, { data: { schema_id: schemaId } })).status()).toBe(200);
  const missing = await member.api.post(`/api/code/projects/${widget.id}/sql-check`, {
    data: { sql: 'SELECT id FROM parcels WHERE status = @status', parameters: [] },
  });
  expect(missing.status()).toBe(422);
  expect(JSON.stringify(await missing.json())).toContain('@status');
  const events = await sqlCheck(member.api, widget.id, 'SELECT id FROM parcels WHERE status = @status', [{ name: '@status', value: 'Аренда' }]);
  const last = events.filter((event) => event.event === 'sql_run').at(-1)?.data.blocks[0];
  expect(last?.status, JSON.stringify(last)).toBe('passed');
  expect(last?.db ?? null).toBeNull();
  expect(last?.result?.rows).toEqual([['1']]);
  expect(events.at(-1)?.event).toBe('done');
});

test('экран «Базы данных» не виден при выключенных подключениях', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/users');
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  await expect(page.getByText('Базы данных', { exact: true })).toHaveCount(0);
});
