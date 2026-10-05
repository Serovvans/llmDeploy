import { describe, expect, it, vi } from 'vitest';

import { fail, liveSse, mockApi, ok, session, sse } from '../test/mockApi';
import { errorText, texts } from '../texts';
import { api, ApiError, closeOpenStreams, isAbort, NetworkError, onSessionSignal, StreamBrokenError } from './client';

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

  it('статус 413 — «Запрос слишком большой»: и без тела JSON (ответ Caddy), и с кодом request_too_large', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Request Entity Too Large', { status: 413 })));
    const fromCaddy = await api.login('a', 'b').catch((error: unknown) => error);
    expect(fromCaddy).toMatchObject({ status: 413, code: 'request_too_large' });
    expect(errorText(fromCaddy)).toBe('Запрос слишком большой. Сократите текст или выберите файл поменьше');

    mockApi({ 'POST /api/auth/login': () => fail(413, 'request_too_large', { details: { max_bytes: 1048576 } }) });
    expect(errorText(await api.login('a', 'b').catch((error: unknown) => error))).toBe(texts.common.requestTooLarge);

    // У `file_too_large` текст свой, с пределом: общий текст его не подменяет.
    mockApi({ 'POST /api/auth/login': () => fail(413, 'file_too_large') });
    expect(errorText(await api.login('a', 'b').catch((error: unknown) => error))).not.toBe(texts.common.requestTooLarge);
  });

  it('поток: отказ до открытия — обычная ошибка; session_ended сообщает о потере сессии', async () => {
    const server = mockApi({
      'POST /api/dialogs/d-1/messages': () => fail(409, 'generation_in_progress'),
      'POST /api/dialogs/d-1/regenerate': () =>
        sse([
          ['start', { user_message_id: 'u', assistant_message_id: 'a' }],
          ['error', { code: 'session_ended', message: 'Сеанс завершён.' }],
        ]),
    });
    const listener = vi.fn();
    const unsubscribe = onSessionSignal(listener);
    const body = { content: 'Вопрос', attachment_ids: [], mode: 'fast', knowledge: 'none' };

    const refused = api.sendMessage('d-1', body, new AbortController().signal);
    await expect(refused.next()).rejects.toMatchObject({ code: 'generation_in_progress' });
    expect(server.calls[0]?.headers).toMatchObject({ 'X-Portal-Csrf': '1', Accept: 'text/event-stream' });

    const events = [];
    for await (const event of api.regenerate('d-1', new AbortController().signal)) {
      events.push(event.type);
    }
    expect(events).toEqual(['start', 'error']);
    expect(listener.mock.calls).toEqual([['unauthenticated']]);
    unsubscribe();
  });

  it('закрытие потоков: «Остановить» и closeOpenStreams обрывают чтение ошибкой отмены', async () => {
    for (const close of ['signal', 'all'] as const) {
      const live = liveSse();
      mockApi({ 'POST /api/dialogs/d-1/regenerate': () => live.response });
      const controller = new AbortController();
      const stream = api.regenerate('d-1', controller.signal);
      live.push(['start', { user_message_id: 'u', assistant_message_id: 'a' }]);
      expect((await stream.next()).value).toMatchObject({ type: 'start' });

      if (close === 'signal') {
        controller.abort();
      } else {
        closeOpenStreams();
      }
      const failure = await stream.next().catch((error: unknown) => error);
      expect(isAbort(failure)).toBe(true);
    }
  });

  it('обрыв соединения посреди потока — сбой сети', async () => {
    const live = liveSse();
    mockApi({ 'POST /api/dialogs/d-1/regenerate': () => live.response });
    const stream = api.regenerate('d-1', new AbortController().signal);
    live.push(['delta', { text: 'Часть' }]);
    await stream.next();
    live.fail();
    await expect(stream.next()).rejects.toBeInstanceOf(StreamBrokenError);

    // Поток, закрытый сервером без завершающего события, — тот же обрыв.
    mockApi({ 'POST /api/dialogs/d-1/regenerate': () => sse([['delta', { text: 'Часть' }]]) });
    const cut = api.regenerate('d-1', new AbortController().signal);
    await cut.next();
    await expect(cut.next()).rejects.toBeInstanceOf(StreamBrokenError);

    // Связь оборвалась до статуса — обычный сбой сети: принят ли вопрос, неизвестно.
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const failure = await api.regenerate('d-1', new AbortController().signal).next().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(NetworkError);
    expect(failure).not.toBeInstanceOf(StreamBrokenError);
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
