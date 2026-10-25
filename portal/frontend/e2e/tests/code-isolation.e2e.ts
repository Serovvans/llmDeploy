/**
 * Изоляция раздела «Код» через API (docs/portal-design.md §10, критерий 37; docs/portal-api.md §7.5.2;
 * CLAUDE.md, пайплайн «Код», правило 5): чужой проект, файл, версия, сборка, архив и правка —
 * `404` и сотруднику, и администратору, в том числе через свой проект с чужим идентификатором;
 * без сессии закрыто всё.
 */
import type { APIRequestContext, APIResponse } from '@playwright/test';

import { newApi } from '../support/accounts';
import { askCode, build, fileOf, getProject, lastAnswerId, simpleProject, type CodeProject } from '../support/code';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { saveSqlSchema } from '../support/portal';

type Call = (api: APIRequestContext) => Promise<APIResponse>;

/** Проект владельца со сборкой, архивом, версией и правкой помощника. */
async function ownerProject(owner: Member) {
  const project = await simpleProject(owner.api, 'Secret');
  expect(must((await build(owner.api, project.id)).archive, 'архив')).not.toBeNull();
  await askCode(owner.api, project.dialog_id, 'Добавь комментарий');
  const messageId = await lastAnswerId(owner.api, project.dialog_id);
  const snapshots = (await (await owner.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items;
  const schemaId = await saveSqlSchema(owner.api, 'Чужая схема', 'CREATE TABLE secret (id int);');
  return { project: await getProject(owner.api, project.id), messageId, snapshotId: must(snapshots[0], 'версия').id as string, schemaId };
}

function calls(data: Awaited<ReturnType<typeof ownerProject>>, own: CodeProject | null): Record<string, Call> {
  const { project, messageId, snapshotId } = data;
  const file = must(project.files[0], 'файл проекта');
  const base = `/api/code/projects/${project.id}`;
  const result: Record<string, Call> = {
    'проект: чтение': (api) => api.get(base),
    'проект: настройки': (api) => api.patch(base, { data: { packages: [] } }),
    'проект: удаление': (api) => api.delete(base),
    'файл: создание': (api) => api.post(`${base}/files`, { data: { path: 'Injected.cs', content: '// x' } }),
    'файл: изменение': (api) => api.patch(`${base}/files/${file.id}`, { data: { revision: file.revision, content: '// x' } }),
    'файл: удаление': (api) => api.delete(`${base}/files/${file.id}`),
    'выгрузка': (api) => api.get(`${base}/export`),
    'версии: список': (api) => api.get(`${base}/snapshots`),
    'версия: возврат': (api) => api.post(`${base}/snapshots/${snapshotId}/restore`),
    'сборка': (api) => api.post(`${base}/build`),
    'архив': (api) => api.get(`${base}/build/archive`),
    'правка: чтение': (api) => api.get(`${base}/edits/${messageId}`),
    'правка: решение': (api) => api.post(`${base}/edits/${messageId}/decision`, { data: { accept: [] } }),
    'диалог помощника: чтение': (api) => api.get(`/api/dialogs/${project.dialog_id}`),
    'диалог помощника: сообщения': (api) => api.get(`/api/dialogs/${project.dialog_id}/messages`),
    'диалог помощника: вопрос': (api) =>
      api.post(`/api/dialogs/${project.dialog_id}/messages`, { data: { content: 'Чужой вопрос', action: 'write', file_ids: null, target: null } }),
    'диалог помощника: память': (api) => api.get(`/api/dialogs/${project.dialog_id}/memory?action=write`),
    'диалог помощника: экспорт': (api) => api.get(`/api/dialogs/${project.dialog_id}/export`),
  };
  if (own) {
    // Свой проект, чужие файл, версия и правка: отбор — по проекту владельца, а не только по идентификатору.
    const mine = `/api/code/projects/${own.id}`;
    result['свой проект, чужой файл: изменение'] = (api) =>
      api.patch(`${mine}/files/${file.id}`, { data: { revision: file.revision, content: '// x' } });
    result['свой проект, чужой файл: удаление'] = (api) => api.delete(`${mine}/files/${file.id}`);
    result['свой проект, чужая версия: возврат'] = (api) => api.post(`${mine}/snapshots/${snapshotId}/restore`);
    result['свой проект, чужая правка: чтение'] = (api) => api.get(`${mine}/edits/${messageId}`);
    result['свой проект, чужая правка: решение'] = (api) => api.post(`${mine}/edits/${messageId}/decision`, { data: { accept: [] } });
  }
  return result;
}

/** Владелец видит свой проект нетронутым: ни одна попытка ничего не изменила. */
async function expectIntact(owner: Member, data: Awaited<ReturnType<typeof ownerProject>>): Promise<void> {
  const now = await getProject(owner.api, data.project.id);
  expect(now.files.map((file) => [file.path, file.revision, file.content])).toEqual(
    data.project.files.map((file) => [file.path, file.revision, file.content]),
  );
  expect(must(now.build, 'сборка').outdated).toBe(false);
  const edit = await (await owner.api.get(`/api/code/projects/${data.project.id}/edits/${data.messageId}`)).json();
  expect(edit.status).toBe('proposed');
}

for (const role of ['employee', 'admin'] as const) {
  test(`чужой проект, файл, версия, сборка, архив и правка — «не найдено» (${role === 'admin' ? 'администратор' : 'сотрудник'})`, async ({
    newMember,
  }) => {
    const owner = await newMember();
    const intruder = await newMember(role);
    const data = await ownerProject(owner);
    const own = await simpleProject(intruder.api, 'Mine');

    for (const [name, call] of Object.entries(calls(data, own))) {
      const response = await call(intruder.api);
      expect(response.status(), `${name}: ${await response.text()}`).toBe(404);
    }
    const list = await (await intruder.api.get('/api/code/projects')).json();
    expect(list.items.map((item: { id: string }) => item.id)).toEqual([own.id]);

    // Чужая схема и чужой файл в своём проекте — как несуществующие.
    const schema = await intruder.api.patch(`/api/code/projects/${own.id}`, { data: { schema_id: data.schemaId } });
    expect(schema.status()).toBe(422);
    expect((await schema.json()).error.code).toBe('schema_not_found');
    const foreignFile = await intruder.api.post(`/api/dialogs/${own.dialog_id}/messages`, {
      data: { content: 'Объясни', action: 'explain', file_ids: [must(data.project.files[0], 'файл').id], target: null },
    });
    expect(foreignFile.status()).toBe(422);
    expect(JSON.stringify((await foreignFile.json()).error)).toContain('unknown_value');

    await expectIntact(owner, data);
    expect(fileOf(await getProject(intruder.api, own.id), 'GpToolFactory.cs').revision).toBe(1);
  });
}

test('без сессии маршруты раздела «Код» закрыты', async ({ newMember }) => {
  const owner = await newMember();
  const data = await ownerProject(owner);
  const anonymous = await newApi();
  const all: Record<string, Call> = {
    ...calls(data, null),
    'список проектов': (api) => api.get('/api/code/projects'),
    'создание проекта': (api) => api.post('/api/code/projects', { data: { source: 'example', name: 'Anon' } }),
    'импорт': (api) =>
      api.post('/api/code/projects/import', { multipart: { name: 'Anon', file: { name: 'a.zip', mimeType: 'application/zip', buffer: Buffer.from('PK') } } }),
    'справочник': (api) => api.get('/api/code/sdk'),
  };
  for (const [name, call] of Object.entries(all)) {
    const response = await call(anonymous);
    expect(response.status(), `${name}: ${await response.text()}`).toBe(401);
  }
  await anonymous.dispose();
  await expectIntact(owner, data);
});
