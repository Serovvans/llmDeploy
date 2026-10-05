/**
 * Критерий 6 (docs/portal-design.md §10) и §8: изоляция данных проверяется через API, минуя
 * интерфейс. Чужой чат, чужой личный документ, чужая схема — `404` и сотруднику, и
 * администратору; маршруты администратора сотруднику закрыты; без сессии закрыто всё.
 */
import type { APIRequestContext, APIResponse } from '@playwright/test';

import { newApi, uniqueSuffix } from '../support/accounts';
import { pngImage, textFile, textPdf } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { addKbDocument, askChat, createDialog, eventsOf, expectError, parseStream } from '../support/portal';

type Call = (api: APIRequestContext) => Promise<APIResponse>;

const MISSING = '00000000-0000-4000-8000-000000000000';

/** Данные владельца: чат с вложением, разбор, личный документ, схема SQL. */
async function ownerData(owner: Member) {
  const mark = uniqueSuffix();
  const chatId = await createDialog(owner.api, 'chat');
  const attachment = await (
    await owner.api.post(`/api/dialogs/${chatId}/attachments`, { multipart: { file: pngImage(`фото-${mark}.png`, mark) } })
  ).json();
  await askChat(owner.api, chatId, `Секретный вопрос ${mark}`, { attachmentIds: [attachment.id] });
  const messages = await (await owner.api.get(`/api/dialogs/${chatId}/messages`)).json();
  const answerId: string = messages.items[0].id;

  const parse = await owner.api.post('/api/docparse', {
    multipart: { file: textPdf(`lease-${mark}.pdf`, [`Private lease ${mark} of the owner only.`]), template_id: 'lease' },
  });
  const docparseId: string = must(eventsOf(parseStream(await parse.text()), 'extraction')[0], 'событие extraction').data.dialog_id;

  const document = await addKbDocument(
    owner.api,
    textFile(`личное-${mark}.txt`, `Личный документ ${mark}: только для владельца.`),
    { scope: 'personal' },
  );
  const schema = await (
    await owner.api.post('/api/sql/schemas', { data: { name: `Схема ${mark}`, content: 'CREATE TABLE t (id int);' } })
  ).json();
  return { mark, chatId, attachmentId: attachment.id as string, answerId, docparseId, documentId: document.id, schemaId: schema.id as string };
}

function foreignCalls(data: Awaited<ReturnType<typeof ownerData>>): Record<string, Call> {
  const message = { content: 'Чужой вопрос', mode: 'fast', knowledge: 'none' };
  const calls: Record<string, Call> = {};
  for (const [name, id] of [
    ['чат', data.chatId],
    ['разбор', data.docparseId],
  ] as const) {
    calls[`${name}: чтение`] = (api) => api.get(`/api/dialogs/${id}`);
    calls[`${name}: сообщения`] = (api) => api.get(`/api/dialogs/${id}/messages`);
    calls[`${name}: новый вопрос`] = (api) => api.post(`/api/dialogs/${id}/messages`, { data: message });
    calls[`${name}: повторная генерация`] = (api) => api.post(`/api/dialogs/${id}/regenerate`);
    calls[`${name}: переименование`] = (api) => api.patch(`/api/dialogs/${id}`, { data: { title: 'Захвачено' } });
    calls[`${name}: экспорт`] = (api) => api.get(`/api/dialogs/${id}/export`);
    calls[`${name}: удаление`] = (api) => api.delete(`/api/dialogs/${id}`);
  }
  calls['чат: экспорт одного ответа'] = (api) => api.get(`/api/dialogs/${data.chatId}/export?message_id=${data.answerId}`);
  calls['чат: файл вложения'] = (api) => api.get(`/api/dialogs/${data.chatId}/attachments/${data.attachmentId}/file`);
  calls['чат: удаление вложения'] = (api) => api.delete(`/api/dialogs/${data.chatId}/attachments/${data.attachmentId}`);
  calls['чат: новое вложение'] = (api) =>
    api.post(`/api/dialogs/${data.chatId}/attachments`, { multipart: { file: textFile('чужое.txt', 'текст') } });
  calls['личный документ: чтение'] = (api) => api.get(`/api/kb/documents/${data.documentId}`);
  calls['личный документ: текст'] = (api) => api.get(`/api/kb/documents/${data.documentId}/text`);
  calls['личный документ: оригинал'] = (api) => api.get(`/api/kb/documents/${data.documentId}/file`);
  calls['личный документ: повторная обработка'] = (api) => api.post(`/api/kb/documents/${data.documentId}/retry`);
  calls['личный документ: удаление'] = (api) => api.delete(`/api/kb/documents/${data.documentId}`);
  calls['схема SQL: чтение'] = (api) => api.get(`/api/sql/schemas/${data.schemaId}`);
  calls['схема SQL: замена'] = (api) => api.put(`/api/sql/schemas/${data.schemaId}`, { data: { name: 'x', content: 'y' } });
  calls['схема SQL: удаление'] = (api) => api.delete(`/api/sql/schemas/${data.schemaId}`);
  return calls;
}

for (const role of ['employee', 'admin'] as const) {
  const who = role === 'admin' ? 'администратору' : 'другому сотруднику';

  test(`чужой чат, разбор, личный документ и схема недоступны ${who}: 404 на каждом маршруте`, async ({ newMember }) => {
    const owner = await newMember();
    const stranger = await newMember(role);
    const data = await ownerData(owner);

    for (const [name, call] of Object.entries(foreignCalls(data))) {
      const response = await call(stranger.api);
      const body = await response.text();
      expect(response.status(), `${name}: ${body}`).toBe(404);
      expect(JSON.parse(body).error.code, name).toBe('not_found');
      // Ответ на чужое не отличается от ответа на несуществующее и ничего не раскрывает.
      expect(body, name).not.toContain(data.mark);
    }

    // В списках чужого нет.
    for (const kind of ['chat', 'docparse', 'sql', 'cogis']) {
      expect((await (await stranger.api.get(`/api/dialogs?kind=${kind}`)).json()).items, kind).toEqual([]);
    }
    expect((await (await stranger.api.get('/api/kb/documents?scope=personal')).json()).items).toEqual([]);
    expect((await (await stranger.api.get(`/api/kb/documents?scope=shared&q=${data.mark}`)).json()).items).toEqual([]);
    expect((await (await stranger.api.get('/api/sql/schemas')).json()).items).toEqual([]);

    // Попытки ничего не изменили у владельца.
    const dialog = await (await owner.api.get(`/api/dialogs/${data.chatId}`)).json();
    expect(dialog.title).not.toBe('Захвачено');
    expect((await (await owner.api.get(`/api/dialogs/${data.chatId}/messages`)).json()).items).toHaveLength(2);
    expect((await owner.api.get(`/api/dialogs/${data.docparseId}`)).status()).toBe(200);
    expect((await owner.api.get(`/api/kb/documents/${data.documentId}/file`)).status()).toBe(200);
    expect((await owner.api.get(`/api/dialogs/${data.chatId}/attachments/${data.attachmentId}/file`)).status()).toBe(200);
    expect((await (await owner.api.get(`/api/sql/schemas/${data.schemaId}`)).json()).name).toBe(`Схема ${data.mark}`);
  });
}

test('ответ на чужой ресурс совпадает с ответом на несуществующий', async ({ newMember }) => {
  const owner = await newMember();
  const stranger = await newMember();
  const chatId = await createDialog(owner.api, 'chat');
  await askChat(owner.api, chatId, 'Вопрос владельца');
  const foreign = await stranger.api.get(`/api/dialogs/${chatId}/messages`);
  const missing = await stranger.api.get(`/api/dialogs/${MISSING}/messages`);
  expect(foreign.status()).toBe(404);
  expect(await foreign.json()).toEqual(await missing.json());
});

test('маршруты администратора закрыты сотруднику', async ({ newMember }) => {
  const employee = await newMember();
  const victim = await newMember();
  const target = `/api/admin/users/${victim.account.id}`;
  const calls: Record<string, Call> = {
    'список пользователей': (api) => api.get('/api/admin/users'),
    'создание пользователя': (api) =>
      api.post('/api/admin/users', { data: { full_name: 'Самозванец', login: `e2e-x-${uniqueSuffix()}`, role: 'admin' } }),
    'правка учётной записи': (api) => api.patch(target, { data: { role: 'admin' } }),
    'повышение самого себя': (api) => api.patch(`/api/admin/users/${employee.account.id}`, { data: { role: 'admin' } }),
    'сброс пароля': (api) => api.post(`${target}/reset-password`),
    'сброс второго фактора': (api) => api.post(`${target}/reset-second-factor`),
    'блокировка': (api) => api.post(`${target}/block`),
    'разблокировка': (api) => api.post(`${target}/unblock`),
    'снятие блокировки входа': (api) => api.post(`${target}/unlock-login`),
  };
  for (const [name, call] of Object.entries(calls)) {
    const response = await call(employee.api);
    const body = await response.text();
    expect(response.status(), `${name}: ${body}`).toBe(403);
    expect(JSON.parse(body).error.code, name).toBe('forbidden');
    expect(body, name).not.toContain(victim.account.login);
  }
  // Учётная запись, на которую покушались, не изменилась.
  const session = await (await victim.api.get('/api/auth/session')).json();
  expect(session).toMatchObject({ step: 'ready', user: { role: 'employee' } });
  expect((await (await employee.api.get('/api/auth/session')).json()).user.role).toBe('employee');
});

test('без сессии закрыты все маршруты API, кроме входа', async ({ newMember }) => {
  const owner = await newMember();
  const data = await ownerData(owner);
  const anonymous = await newApi();
  const calls: Record<string, Call> = {
    ...foreignCalls(data),
    'сессия': (api) => api.get('/api/auth/session'),
    'конфигурация': (api) => api.get('/api/config'),
    'список чатов': (api) => api.get('/api/dialogs?kind=chat'),
    'создание чата': (api) => api.post('/api/dialogs', { data: { kind: 'chat' } }),
    'запуск разбора': (api) =>
      api.post('/api/docparse', { multipart: { file: pngImage('скан.png', 'abcdef'), template_id: 'free' } }),
    'общая база': (api) => api.get('/api/kb/documents?scope=shared'),
    'загрузка в базу': (api) =>
      api.post('/api/kb/documents', { multipart: { file: textFile('а.txt', 'текст'), scope: 'shared', is_cogis: 'false' } }),
    'наличие документации CoGIS': (api) => api.get('/api/kb/cogis-documentation'),
    'схемы SQL': (api) => api.get('/api/sql/schemas'),
    'пользователи': (api) => api.get('/api/admin/users'),
    'блокировка пользователя': (api) => api.post(`/api/admin/users/${owner.account.id}/block`),
    'смена пароля': (api) => api.post('/api/auth/password', { data: { new_password: 'новый-пароль-без-сессии' } }),
  };
  for (const [name, call] of Object.entries(calls)) {
    const response = await call(anonymous);
    const body = await response.text();
    expect(response.status(), `${name}: ${body}`).toBe(401);
    expect(JSON.parse(body).error.code, name).toBe('unauthenticated');
    expect(body, name).not.toContain(data.mark);
  }
  // Маршруты шага входа без сессии сообщают, что время на шаг вышло.
  await expectError(await anonymous.post('/api/auth/second-factor', { data: { code: '000000' } }), 401, 'login_step_expired');
  await expectError(await anonymous.post('/api/auth/second-factor/setup'), 401, 'login_step_expired');
  await anonymous.dispose();
});

test('изменяющий запрос без заголовка защиты от CSRF отклоняется и не выполняется', async ({ newMember }) => {
  const member = await newMember();
  const bare = await newApi({ csrf: false });
  // Та же сессия, но без заголовка — так запрос выглядел бы с чужого сайта.
  const cookies = (await member.api.storageState()).cookies;
  const cookie = cookies.map((item) => `${item.name}=${item.value}`).join('; ');

  await expectError(await bare.post('/api/dialogs', { headers: { cookie }, data: { kind: 'chat' } }), 403, 'csrf_check_failed');
  await expectError(await bare.post('/api/auth/logout', { headers: { cookie } }), 403, 'csrf_check_failed');
  await expectError(
    await bare.post('/api/auth/login', { data: { login: member.account.login, password: member.account.password } }),
    403,
    'csrf_check_failed',
  );
  // Чтение заголовка не требует, а сессия после отклонённого выхода жива.
  expect((await bare.get('/api/auth/session', { headers: { cookie } })).status()).toBe(200);
  expect((await member.api.get('/api/auth/session')).status()).toBe(200);
  await bare.dispose();
});

test('cookie сессии: HttpOnly, Secure, SameSite=Strict, только для /api', async ({ newMember }) => {
  const member = await newMember();
  const cookies = (await member.api.storageState()).cookies;
  expect(cookies).toHaveLength(1);
  expect(cookies[0]).toMatchObject({
    name: 'portal_session',
    path: '/api',
    httpOnly: true,
    secure: true,
    sameSite: 'Strict',
  });
});
