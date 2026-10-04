import { describe, expect, it, vi } from 'vitest';

import { fail, mockApi, ok, session } from '../test/mockApi';
import { api, ApiError, NetworkError, onSessionSignal } from './client';

describe('клиент API', () => {
  it('добавляет заголовок защиты от CSRF ко всем запросам, кроме GET', async () => {
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'POST /api/auth/login': () => ok(session('second_factor')),
      'POST /api/auth/logout': () => ok(),
      'PATCH /api/admin/users/u-2': () => ok({}),
    });

    await api.getSession();
    await api.login('ivanov', 'secret');
    await api.logout();
    await api.updateUser('u-2', { role: 'admin' });

    const [get, post, emptyPost, patch] = server.calls;
    expect(get?.headers['X-Portal-Csrf']).toBeUndefined();
    expect(post?.headers['X-Portal-Csrf']).toBe('1');
    expect(emptyPost?.headers['X-Portal-Csrf']).toBe('1');
    expect(patch?.headers['X-Portal-Csrf']).toBe('1');
    expect(post?.body).toEqual({ login: 'ivanov', password: 'secret' });
    expect(emptyPost?.body).toBeUndefined();
  });

  it('возвращает тело ответа, а для 204 — ничего', async () => {
    mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'POST /api/auth/logout': () => ok(),
    });

    await expect(api.getSession()).resolves.toMatchObject({ step: 'ready' });
    await expect(api.logout()).resolves.toBeUndefined();
  });

  it('передаёт ровно одно поле кода: `code` или `backup_code`', async () => {
    const server = mockApi({
      'POST /api/auth/second-factor': () => ok({ session: session('ready'), backup_code_used: false }),
    });

    await api.submitCode('123456');
    await api.submitBackupCode('4F7K-92QD');

    expect(server.calls.map((call) => call.body)).toEqual([{ code: '123456' }, { backup_code: '4F7K-92QD' }]);
  });

  it('не передаёт `current_password` при обязательной смене и передаёт при добровольной', async () => {
    const server = mockApi({ 'POST /api/auth/password': () => ok(session('ready')) });

    await api.changeTemporaryPassword('новый пароль из слов');
    await api.changeOwnPassword('старый', 'новый пароль из слов');

    expect(server.calls[0]?.body).toEqual({ new_password: 'новый пароль из слов' });
    expect(server.calls[1]?.body).toEqual({ new_password: 'новый пароль из слов', current_password: 'старый' });
  });

  it('разбирает отказ в формате контракта: код, поля, подробности', async () => {
    mockApi({
      'POST /api/auth/password': () =>
        fail(422, 'validation_error', {
          fields: [{ field: 'new_password', code: 'password_too_short', message: 'Пароль короче 12 символов.' }],
        }),
      'POST /api/auth/login': () => fail(429, 'login_locked', { details: { retry_after_seconds: 300 } }),
    });

    const validation = await api.changeTemporaryPassword('x').catch((error: unknown) => error);
    expect(validation).toBeInstanceOf(ApiError);
    expect(validation).toMatchObject({
      status: 422,
      code: 'validation_error',
      fields: [{ field: 'new_password', code: 'password_too_short' }],
      details: {},
    });

    const locked = await api.login('a', 'b').catch((error: unknown) => error);
    expect(locked).toMatchObject({ status: 429, code: 'login_locked', details: { retry_after_seconds: 300 }, fields: [] });
  });

  it('отказ не в формате контракта превращает в общий сбой сервера', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('<html>Bad Gateway</html>', { status: 502 })));
    await expect(api.getSession()).rejects.toMatchObject({ code: 'internal_error', status: 502 });

    vi.stubGlobal('fetch', vi.fn(async () => new Response('', { status: 503 })));
    await expect(api.getSession()).rejects.toMatchObject({ code: 'service_unavailable' });
  });

  it('сбой сети — отдельная ошибка', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(api.getSession()).rejects.toBeInstanceOf(NetworkError);
  });

  it('сообщает слушателю о потере сессии и смене шага, но не о прочих отказах', async () => {
    mockApi({
      'GET /api/auth/session': () => fail(401, 'unauthenticated'),
      'GET /api/config': () => fail(403, 'login_step_required', { details: { step: 'second_factor' } }),
      'POST /api/auth/login': () => fail(401, 'invalid_credentials'),
      'POST /api/auth/second-factor': () => fail(401, 'login_step_expired'),
    });
    const listener = vi.fn();
    const unsubscribe = onSessionSignal(listener);

    await api.getSession().catch(() => undefined);
    await api.getConfig().catch(() => undefined);
    await api.login('a', 'b').catch(() => undefined);
    await api.submitCode('123456').catch(() => undefined);
    expect(listener.mock.calls).toEqual([['unauthenticated'], ['login_step_required']]);

    unsubscribe();
    await api.getSession().catch(() => undefined);
    expect(listener).toHaveBeenCalledTimes(2);
  });

  it('запрашивает пользователей по страницам с отбором и сортировкой по ФИО', async () => {
    const server = mockApi({ 'GET /api/admin/users': () => ok({ items: [], page: 2, page_size: 50, total: 0 }) });
    const fetchMock = vi.mocked(fetch);

    await api.listUsers({ page: 2, q: 'иван', order: 'desc' });
    await api.listUsers({ page: 1, q: '', order: 'asc' });

    expect(server.calls).toHaveLength(2);
    const first = new URL(String(fetchMock.mock.calls[0]?.[0]), 'http://localhost').searchParams;
    expect(Object.fromEntries(first)).toEqual({ page: '2', page_size: '50', sort: 'full_name', order: 'desc', q: 'иван' });
    const second = new URL(String(fetchMock.mock.calls[1]?.[0]), 'http://localhost').searchParams;
    expect(second.has('q')).toBe(false);
  });
});
