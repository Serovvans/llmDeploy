import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Attachment, Dialog, DocumentText, KbDocument, Message, Source } from '../api/types';
import { CONFIG, fail, liveSse, mockApi, ok, session, sse } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { texts } from '../texts';

const t = texts.chat;
const ID = 'd-1';
const START = ['start', { user_message_id: 'u-1', assistant_message_id: 'a-1' }] as [string, object];

function dialog(patch: Partial<Dialog> = {}): Dialog {
  const now = new Date().toISOString();
  return { id: ID, kind: 'chat', title: 'Аренда участка под ЛЭП', created_at: now, updated_at: now, ...patch };
}

function message(patch: Partial<Message>): Message {
  return {
    id: 'a-1',
    role: 'assistant',
    content: '',
    status: 'complete',
    error_code: null,
    reasoning: null,
    reasoning_seconds: null,
    attachments: [],
    sources: null,
    sources_found: null,
    sql_check: null,
    sql_run: null,
    sql_dangers: null,
    dropped_messages: 0,
    created_at: '2026-10-05T09:01:00Z',
    ...patch,
  };
}

const QUESTION = message({ id: 'u-1', role: 'user', content: 'Какой срок аренды?', created_at: '2026-10-05T09:00:00Z' });

/** Чат с историей: `messages` — по возрастанию времени, сервер отдаёт от новых к старым. */
function setupChat(messages: Message[], path = `/chat/${ID}`) {
  const server = mockApi({
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/dialogs': () => ok({ items: [dialog()], next_cursor: null }),
    [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [...messages].reverse(), next_cursor: null }),
  });
  renderApp(path);
  return { server, user: userEvent.setup({ applyAccept: false }) };
}

/** Сигнал отмены последнего открытого потока ответа. */
function lastStreamSignal(): AbortSignal | null | undefined {
  const calls = vi.mocked(fetch).mock.calls.filter(([input]) => /\/(messages|regenerate)$/.test(String(input)));
  return calls.filter(([, init]) => init?.method === 'POST').at(-1)?.[1]?.signal;
}

function field(): HTMLElement {
  return screen.getByPlaceholderText(t.composer.placeholder);
}

function path(): string {
  return screen.getByTestId('path').textContent ?? '';
}

describe('чат: отправка и поток ответа', () => {
  it('новый чат создаётся при первой отправке; ответ идёт по событиям, название — из события title', async () => {
    const user = userEvent.setup();
    const live = liveSse();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'POST /api/dialogs': () => ok(dialog({ title: null }), 201),
      [`POST /api/dialogs/${ID}/messages`]: () => live.response,
    });
    renderApp('/chat');

    expect(await screen.findByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    await waitFor(() => expect(field()).toHaveFocus());
    expect(server.callsTo('POST /api/dialogs')).toHaveLength(0);

    await user.type(field(), 'Какой срок аренды?{Enter}');
    await waitFor(() => expect(path()).toBe(`/chat/${ID}`));
    expect(server.callsTo('POST /api/dialogs')[0]?.body).toEqual({ kind: 'chat' });
    expect((await screen.findAllByText(t.sending)).length).toBeGreaterThan(0);
    expect(screen.getByText('Какой срок аренды?')).toBeInTheDocument();
    expect(field()).toHaveValue('');
    expect(screen.getByRole('button', { name: t.composer.stop })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.composer.send })).not.toBeInTheDocument();

    live.push(START, ['reasoning_delta', { text: 'Нужно найти срок в договоре…' }]);
    expect(await screen.findByText(/Модель думает… \d+ с/)).toBeInTheDocument();
    expect(screen.queryByText('Нужно найти срок в договоре…')).not.toBeInTheDocument();

    live.push(['delta', { text: 'Срок аренды — ' }], ['title', { title: 'Срок аренды по договору' }]);
    expect(await screen.findByText('Срок аренды —')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { level: 1, name: 'Срок аренды по договору' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Срок аренды по договору' })).toHaveAttribute('aria-current', 'page');

    live.push(['delta', { text: '**49 лет**.' }], ['done', { status: 'complete' }]);
    live.close();
    expect(await screen.findByText('49 лет')).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: t.composer.send })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t.regenerate })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t.copy })).toBeInTheDocument();

    // Размышления свёрнуты; раскрываются по нажатию.
    const thinking = screen.getByRole('button', { name: /^Размышления · \d+ с$/ });
    expect(thinking).toHaveAttribute('aria-expanded', 'false');
    await user.click(thinking);
    expect(screen.getByText('Нужно найти срок в договоре…')).toBeInTheDocument();

    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]).toMatchObject({
      body: { content: 'Какой срок аренды?', attachment_ids: [], mode: 'fast', collections: [] },
      headers: { 'X-Portal-Csrf': '1' },
    });
  });

  it('пустое поле не отправляется; Shift+Enter — новая строка; режим ответа запоминается', async () => {
    const { server, user } = setupChat([]);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.click(screen.getByRole('button', { name: t.composer.send }));
    expect(screen.getByRole('alert')).toHaveTextContent(t.composer.enterQuestion);
    expect(field()).toHaveFocus();

    await user.type(field(), 'Первая{Shift>}{Enter}{/Shift}вторая');
    expect(field()).toHaveValue('Первая\nвторая');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByText(t.composer.hint)).toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: t.composer.modeThorough }));
    expect(window.localStorage.getItem('portal.chat.mode')).toBe('thorough');
    server.on(`POST /api/dialogs/${ID}/messages`, () => sse([START, ['done', { status: 'complete' }]]));
    await user.click(screen.getByRole('button', { name: t.composer.send }));
    await waitFor(() =>
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({
        content: 'Первая\nвторая',
        mode: 'thorough',
      }),
    );
  });

  it('остановка закрывает соединение: текст остаётся, «Ответ остановлен», можно ответить заново', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Долгий вопрос{Enter}');
    live.push(START, ['delta', { text: 'Начало ответа' }]);
    expect(await screen.findByText('Начало ответа')).toBeInTheDocument();
    const signal = lastStreamSignal();

    // Пока идёт ответ, Enter не отправляет.
    await user.type(field(), 'Следующий{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.composer.waitAnswer);
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: t.composer.stop }));
    expect(await screen.findByText(t.stopped, { selector: 'p:not([role])' })).toBeInTheDocument();
    expect(signal?.aborted).toBe(true);
    expect(screen.getByText('Начало ответа')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t.regenerate })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t.composer.send })).toBeInTheDocument();
    expect(field()).toHaveValue('Следующий');
    expect(screen.queryByText(t.composer.waitAnswer)).not.toBeInTheDocument();

    // «Ответить заново» — запрос повторной генерации без тела.
    server.on(`POST /api/dialogs/${ID}/regenerate`, () =>
      sse([START, ['delta', { text: 'Новый ответ' }], ['done', { status: 'complete' }]]),
    );
    await user.click(screen.getByRole('button', { name: t.regenerate }));
    expect(await screen.findByText('Новый ответ')).toBeInTheDocument();
    expect(screen.queryByText('Начало ответа')).not.toBeInTheDocument();
    expect(server.callsTo(`POST /api/dialogs/${ID}/regenerate`)[0]?.body).toBeUndefined();
  });

  it('Esc в поле во время ответа — то же, что «Остановить»', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    live.push(START, ['reasoning_delta', { text: 'Думаю' }]);
    await screen.findByText(/Модель думает… \d+ с/);
    await user.type(field(), '{Escape}');
    expect(await screen.findByText(t.stopped, { selector: 'p:not([role])' })).toBeInTheDocument();
  });

  it('обрыв сети во время ответа: «Ответ остановлен» и уведомление о связи', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    live.push(START, ['delta', { text: 'Часть ответа' }]);
    await screen.findByText('Часть ответа');
    // Вопрос принят: лента перечитывается и показывает сохранённое сервером.
    server.on(`GET /api/dialogs/${ID}/messages`, () =>
      ok({ items: [message({ status: 'stopped', content: 'Часть ответа, сохранённая сервером' }), { ...QUESTION, content: 'Вопрос' }], next_cursor: null }),
    );
    live.fail();
    expect(await screen.findByText(t.stopped, { selector: 'p:not([role])' })).toBeInTheDocument();
    expect(await screen.findByText(texts.common.noConnection)).toBeInTheDocument();
    expect(await screen.findByText('Часть ответа, сохранённая сервером')).toBeInTheDocument();
    expect(field()).toHaveValue('');
  });

  it('статус 200 и обрыв до первого события: вопрос принят — текст в поле не возвращается', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Принятый вопрос{Enter}');
    await screen.findAllByText(t.sending);
    server.on(`GET /api/dialogs/${ID}/messages`, () =>
      ok({ items: [message({ status: 'stopped' }), { ...QUESTION, content: 'Принятый вопрос' }], next_cursor: null }),
    );
    live.fail();

    expect(await screen.findByText(texts.common.noConnection)).toBeInTheDocument();
    await waitFor(() => expect(server.callsTo(`GET /api/dialogs/${ID}/messages`).length).toBeGreaterThan(1));
    await waitFor(() => expect(screen.getByText(t.stopped, { selector: 'p:not([role])' })).toBeInTheDocument());
    expect(screen.getByText('Принятый вопрос')).toBeInTheDocument();
    expect(field()).toHaveValue('');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it.each([
    ['вопрос в ленте есть', true, false],
    ['вопроса в ленте нет', false, false],
    ['ленту перечитать не удалось', false, true],
  ])('связь оборвалась до статуса, %s', async (_name, saved, refreshFails) => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/messages`, () => {
      throw new TypeError('Failed to fetch');
    });
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    server.on(`GET /api/dialogs/${ID}/messages`, () => {
      if (refreshFails) {
        throw new TypeError('Failed to fetch');
      }
      const items = saved ? [message({ status: 'stopped' }), { ...QUESTION, content: 'Вопрос без ответа' }] : [];
      return ok({ items, next_cursor: null });
    });

    await user.type(field(), 'Вопрос без ответа{Enter}');

    if (saved) {
      expect(await screen.findByText(t.stopped, { selector: 'p:not([role])' })).toBeInTheDocument();
      expect(field()).toHaveValue('');
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    } else {
      expect(await screen.findByRole('alert')).toHaveTextContent(texts.common.noConnection);
      expect(field()).toHaveValue('Вопрос без ответа');
      expect(screen.getByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    }
  });

  it('«Ответить заново» и связь оборвалась до статуса: показана лента как на сервере, а не прежний ответ', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Прежний ответ' })]);
    server.on(`POST /api/dialogs/${ID}/regenerate`, () => {
      throw new TypeError('Failed to fetch');
    });
    await screen.findByText('Прежний ответ');
    // Сервер запрос принял: прежний ответ удалён, новый сохранён как остановленный.
    server.on(`GET /api/dialogs/${ID}/messages`, () =>
      ok({ items: [message({ id: 'a-2', status: 'stopped', content: 'Новый ответ с сервера' }), QUESTION], next_cursor: null }),
    );

    await user.click(screen.getByRole('button', { name: t.regenerate }));
    expect(await screen.findByText('Новый ответ с сервера')).toBeInTheDocument();
    expect(screen.queryByText('Прежний ответ')).not.toBeInTheDocument();
    expect(await screen.findByText(texts.common.noConnection)).toBeInTheDocument();
  });

  it('«Ответить заново» без конфигурации: отказ сервера возвращает прежний ответ с общим текстом', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Прежний ответ' })]);
    server.on('GET /api/config', () => fail(503, 'service_unavailable'));
    server.on(`POST /api/dialogs/${ID}/regenerate`, () => fail(409, 'nothing_to_regenerate'));

    await user.click(await screen.findByRole('button', { name: t.regenerate }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Сообщение сервера: nothing_to_regenerate.');
    expect(screen.getByText('Прежний ответ')).toBeInTheDocument();
    expect(screen.queryByText(t.sending)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: t.regenerate })).toBeInTheDocument();
  });

  it('возврат вопроса в поле не затирает набранное за время ожидания: прежний текст — перед ним', async () => {
    const { server, user } = setupChat([]);
    let refuse: (() => void) | undefined;
    server.on(
      `POST /api/dialogs/${ID}/messages`,
      () => new Promise((resolve) => (refuse = () => resolve(fail(409, 'generation_in_progress')))),
    );
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Первый вопрос{Enter}');
    await screen.findAllByText(t.sending);
    await user.type(field(), 'Уже набираю второй');
    refuse?.();

    expect(await screen.findByText(texts.dialogs.chat.inProgress)).toBeInTheDocument();
    expect(field()).toHaveValue('Первый вопрос\n\nУже набираю второй');
  });

  it('чат удалён в другой вкладке во время ответа: новый чат, строка убрана, набранный текст остаётся', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    live.push(START, ['delta', { text: 'Идёт ответ' }]);
    await screen.findByText('Идёт ответ');
    await user.type(field(), 'Следующий вопрос');

    live.push(['error', { code: 'dialog_deleted', message: 'Диалог удалён.' }]);
    live.close();
    await waitFor(() => expect(path()).toBe('/chat'));
    expect(await screen.findByText(t.remove.elsewhere)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'Аренда участка под ЛЭП' })).not.toBeInTheDocument();
    expect(screen.queryByText('Идёт ответ')).not.toBeInTheDocument();
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    expect(field()).toHaveValue('Следующий вопрос');
  });

  it('отказ до потока у «Ответить заново»: текст под панелью запроса, прежний ответ на месте', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Прежний ответ' })]);
    server.on(`POST /api/dialogs/${ID}/regenerate`, () => fail(409, 'generation_in_progress'));

    await user.click(await screen.findByRole('button', { name: t.regenerate }));
    expect(await screen.findByRole('alert')).toHaveTextContent(texts.dialogs.chat.inProgress);
    expect(screen.getByText('Прежний ответ')).toBeInTheDocument();
  });

  it('ошибка посреди потока: часть текста остаётся, заметка с «Повторить»', async () => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/messages`, () =>
      sse([START, ['delta', { text: 'Половина' }], ['error', { code: 'model_unavailable', message: 'Сбой.' }]]),
    );
    server.on(`POST /api/dialogs/${ID}/regenerate`, () =>
      sse([START, ['error', { code: 'model_overloaded', message: 'Занято.' }]]),
    );
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent(t.errors.broken);
    expect(screen.getByText('Половина')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByText(t.errors.overloaded)).toBeInTheDocument();
    expect(server.callsTo(`POST /api/dialogs/${ID}/regenerate`)).toHaveLength(1);
  });

  it.each([
    [409, 'generation_in_progress', texts.dialogs.chat.inProgress],
    [422, 'message_too_long', t.errors.tooLong],
    [413, 'request_too_large', texts.common.requestTooLarge],
  ])('отказ до потока %s %s: текст под панелью запроса, вопрос остаётся в поле', async (status, code, text) => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/messages`, () => fail(status, code));
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Мой вопрос{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(field()).toHaveValue('Мой вопрос');
    expect(screen.getByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: t.composer.send })).toBeInTheDocument();
  });

  it('событие session_ended переводит на вход с «Сеанс завершён»', async () => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/messages`, () =>
      sse([START, ['delta', { text: 'Часть' }], ['error', { code: 'session_ended', message: 'Сеанс завершён.' }]]),
    );
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    await waitFor(() => expect(path()).toBe('/login'));
    expect(screen.getByRole('status')).toHaveTextContent(texts.common.sessionEnded);
  });

  it('выход закрывает открытый поток до запроса выхода', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    const order: string[] = [];
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    server.on('POST /api/auth/logout', () => {
      order.push(`logout:${lastStreamSignal()?.aborted ? 'поток закрыт' : 'поток открыт'}`);
      return ok();
    });
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    live.push(START, ['delta', { text: 'Идёт ответ' }]);
    await screen.findByText('Идёт ответ');

    await user.click(screen.getByRole('button', { name: texts.nav.profile }));
    await user.click(menuItems().find((item) => item.textContent === texts.nav.logout) as HTMLElement);
    await waitFor(() => expect(path()).toBe('/login'));
    expect(order).toEqual(['logout:поток закрыт']);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('уход в другой раздел поток не закрывает: ответ виден при возвращении', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    live.push(START, ['delta', { text: 'Первая часть' }]);
    await screen.findByText('Первая часть');

    await user.click(screen.getByRole('link', { name: texts.nav.knowledge }));
    await screen.findByRole('heading', { level: 1, name: texts.sections.knowledge });
    live.push(['delta', { text: ' и вторая' }], ['done', { status: 'complete' }]);
    live.close();

    await user.click(screen.getByRole('link', { name: texts.nav.chat }));
    await user.click(await screen.findByRole('link', { name: 'Аренда участка под ЛЭП' }));
    expect(await screen.findByText('Первая часть и вторая')).toBeInTheDocument();
    expect(lastStreamSignal()?.aborted).toBe(false);
  });
});

describe('чат: ответ после перезагрузки', () => {
  it.each([
    [{ status: 'stopped', content: 'Часть' }, t.stopped],
    [{ status: 'length_limit', content: 'Длинный текст' }, t.lengthLimit],
    [{ status: 'length_limit', content: '' }, t.lengthLimitEmpty],
    [{ status: 'error', error_code: 'model_unavailable', content: '' }, t.errors.noAnswer],
    [{ status: 'error', error_code: 'internal_error', content: 'Часть' }, t.errors.broken],
    [{ status: 'error', error_code: 'interrupted', content: '' }, t.errors.broken],
    [{ status: 'error', error_code: 'session_ended', content: 'Часть' }, t.errors.broken],
    [{ status: 'error', error_code: 'model_overloaded', content: '' }, t.errors.overloaded],
    [{ status: 'error', error_code: 'generation_timeout', content: '' }, t.errors.timeout],
    [{ status: 'error', error_code: 'knowledge_unavailable', content: '' }, t.errors.knowledge],
    [{ status: 'error', error_code: 'message_too_long', content: '' }, t.errors.tooLong],
  ] as const)('%o', async (patch, text) => {
    setupChat([QUESTION, message(patch)]);

    expect(await screen.findByText(text, { selector: 'p:not([role]), span' })).toBeInTheDocument();
    if (patch.status === 'error') {
      expect(screen.getByRole('button', { name: texts.common.retry })).toBeInTheDocument();
    }
    if (patch.content) {
      expect(screen.getByText(patch.content)).toBeInTheDocument();
    }
  });

  it('ответ формируется в другой вкладке: индикатор, панель без «Остановить», отправка недоступна', async () => {
    const { server, user } = setupChat([QUESTION, message({ status: 'streaming', content: '' })]);

    expect(await screen.findByText(t.stillForming)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.composer.stop })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.regenerate })).not.toBeInTheDocument();

    await user.type(field(), 'Ещё вопрос{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.composer.waitAnswer);
    expect(server.calls.some((call) => call.method === 'POST')).toBe(false);
  });

  it('размышления: с длительностью, без неё, без блока', async () => {
    setupChat([
      QUESTION,
      message({ id: 'a-0', content: 'Первый', reasoning: 'Мысли', reasoning_seconds: 12 }),
      message({ id: 'a-1', content: 'Второй', reasoning: 'Мысли', reasoning_seconds: null }),
      message({ id: 'a-2', content: 'Третий', reasoning: null }),
    ]);

    expect(await screen.findByRole('button', { name: 'Размышления · 12 с' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Размышления' })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Размышления/ })).toHaveLength(2);
    // «Ответить заново» — только у последнего ответа.
    expect(screen.getAllByRole('button', { name: t.regenerate })).toHaveLength(1);
  });

  it('заметка о длинном диалоге зависит от последнего ответа; ранние сообщения подгружаются', async () => {
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'GET /api/dialogs': () => ok({ items: [dialog()], next_cursor: null }),
      [`GET /api/dialogs/${ID}/messages`]: (_body, url) =>
        url.searchParams.get('cursor') === 'older'
          ? ok({ items: [message({ id: 'a-0', content: 'Ранний ответ' })], next_cursor: null })
          : ok({ items: [message({ content: 'Поздний ответ', dropped_messages: 3 }), QUESTION], next_cursor: 'older' }),
    });
    renderApp(`/chat/${ID}`);

    expect(await screen.findByText(texts.dialogs.chat.longDialog)).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: t.earlier }));
    expect(await screen.findByText('Ранний ответ')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.earlier })).not.toBeInTheDocument();
    expect(server.callsTo(`GET /api/dialogs/${ID}/messages`)).toHaveLength(2);
  });
});

describe('чат: ответ модели — недоверенные данные', () => {
  const HOSTILE = [
    'Текст <script>window.hacked = true</script> и <img src="x" onerror="window.hacked = true">.',
    '',
    '<iframe src="https://evil.example"></iframe>',
    '',
    '![схема](https://evil.example/pixel.png) [ссылка](https://example.org/doc) [ловушка](javascript:alert(1))',
    '',
    '| Участок | Площадь |',
    '|---|---|',
    '| 77:01 | 12 га |',
    '',
    '```sql',
    "SELECT cad_number FROM parcels WHERE area > 10 -- комментарий",
    '```',
  ].join('\n');

  it.each(['light', 'dark'] as const)('HTML из ответа не исполняется и не вставляется в страницу (тема %s)', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    setupChat([QUESTION, message({ content: HOSTILE })]);

    const table = await screen.findByRole('table');
    const main = screen.getByRole('main');
    expect(main.querySelector('script')).toBeNull();
    expect(main.querySelector('iframe[src*="evil"]')).toBeNull();
    expect(main.querySelector('img')).toBeNull();
    expect((window as unknown as { hacked?: boolean }).hacked).toBeUndefined();
    // Разметка из ответа видна как текст.
    expect(main).toHaveTextContent('<script>window.hacked = true</script>');
    // Картинка по внешнему адресу не загружается — показан адрес.
    expect(main).toHaveTextContent('схема — https://evil.example/pixel.png');

    const link = screen.getByRole('link', { name: 'ссылка' });
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.getByText('ловушка').closest('a')?.getAttribute('href') ?? '').not.toContain('javascript');

    expect(within(table).getByRole('columnheader', { name: 'Площадь' })).toBeInTheDocument();
    expect(within(table).getByRole('cell', { name: '12 га' })).toBeInTheDocument();

    // Блок кода: название языка, «Скопировать», подсветка классами.
    const code = main.querySelector('pre');
    expect(code).toHaveTextContent('SELECT cad_number FROM parcels WHERE area > 10 -- комментарий');
    expect(code?.querySelector('.hljs-keyword')).toHaveTextContent('SELECT');
    expect(code?.querySelector('.hljs-comment')).toHaveTextContent('-- комментарий');
    expect(within(code?.parentElement as HTMLElement).getByText('SQL')).toBeInTheDocument();
    expect(within(code?.parentElement as HTMLElement).getByRole('button', { name: t.copy })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(theme);
  });

  it('вопрос выводится как введён: без Markdown и без разметки', async () => {
    setupChat([message({ id: 'u-1', role: 'user', content: '**не жирный** <b>и не тег</b>\nвторая строка' })]);

    const question = await screen.findByText(/не жирный/);
    expect(question).toHaveTextContent('**не жирный** <b>и не тег</b> вторая строка');
    expect(question.querySelector('b, strong')).toBeNull();
  });

  it('блок кода без языка — «Текст»; неизвестный язык — без подсветки', async () => {
    setupChat([QUESTION, message({ content: '```\nпросто текст\n```\n\n```brainfuck\n+++\n```' })]);

    expect(await screen.findByText('Текст')).toBeInTheDocument();
    expect(screen.getByText('brainfuck')).toBeInTheDocument();
    expect(screen.getByRole('main').querySelector('[class*="hljs-"]')).toBeNull();
  });
});

describe('чат: вложения', () => {
  const PDF: Attachment = { id: 'f-1', file_name: 'договор.pdf', media_type: 'application/pdf', page_count: 3, image_count: 0, duration_seconds: null, transcription: null, created_at: '' };
  const PNG: Attachment = { id: 'f-2', file_name: 'скан.png', media_type: 'image/png', page_count: 1, image_count: 1, duration_seconds: null, transcription: null, created_at: '' };

  function fileInput(): HTMLInputElement {
    return document.querySelector('input[type="file"]') as HTMLInputElement;
  }

  it('файл загружается сразу; миниатюра — с адреса файла на портале; отправка передаёт вложения', async () => {
    const { server, user } = setupChat([]);
    const uploads = [PDF, PNG];
    server.on(`POST /api/dialogs/${ID}/attachments`, () => ok(uploads.shift(), 201));
    server.on(`POST /api/dialogs/${ID}/messages`, () => sse([START, ['done', { status: 'complete' }]]));
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    await user.upload(fileInput(), new File(['%PDF-'], 'договор.pdf', { type: 'application/pdf' }));
    expect(await screen.findByText('· 3 стр.')).toBeInTheDocument();
    const upload = server.callsTo(`POST /api/dialogs/${ID}/attachments`)[0];
    expect(upload?.headers['X-Portal-Csrf']).toBe('1');
    expect((upload?.body as FormData).get('file')).toBeInstanceOf(File);

    await user.upload(fileInput(), new File(['png'], 'скан.png', { type: 'image/png' }));
    await waitFor(() => expect(screen.getByRole('main').querySelector('img')).not.toBeNull());
    const thumbnail = screen.getByRole('main').querySelector('img') as HTMLImageElement;
    expect(thumbnail.getAttribute('src')).toBe(`/api/dialogs/${ID}/attachments/f-2/file`);

    // Вопрос без текста, но с вложениями отправляется.
    await user.click(screen.getByRole('button', { name: t.composer.send }));
    await waitFor(() =>
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({
        content: '',
        attachment_ids: ['f-1', 'f-2'],
      }),
    );
    // У отправленных вложений крестика нет.
    await waitFor(() => expect(screen.queryByRole('button', { name: t.files.remove('договор.pdf') })).not.toBeInTheDocument());
    expect(screen.getByText('договор.pdf')).toBeInTheDocument();
  });

  it('первое вложение в новом чате создаёт чат', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'POST /api/dialogs': () => ok(dialog({ title: null }), 201),
      [`POST /api/dialogs/${ID}/attachments`]: () => ok(PDF, 201),
    });
    renderApp('/chat');
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    await user.upload(fileInput(), new File(['%PDF-'], 'договор.pdf', { type: 'application/pdf' }));
    await waitFor(() => expect(path()).toBe(`/chat/${ID}`));
    expect(await screen.findByText('· 3 стр.')).toBeInTheDocument();
    expect(server.callsTo('POST /api/dialogs')).toHaveLength(1);

    // Строка истории появляется только после первого отправленного вопроса.
    const history = within(screen.getByRole('complementary', { name: t.history }));
    expect(history.queryByRole('link')).not.toBeInTheDocument();
    server.on(`POST /api/dialogs/${ID}/messages`, () => sse([START, ['done', { status: 'complete' }]]));
    await user.click(screen.getByRole('button', { name: t.composer.send }));
    expect(await history.findByRole('link', { name: t.newChat })).toBeInTheDocument();
  });

  it('чат без сообщений, открытый по адресу, попадает в историю после первого вопроса', async () => {
    const user = userEvent.setup();
    mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
      [`GET /api/dialogs/${ID}`]: () => ok(dialog({ title: null })),
      [`POST /api/dialogs/${ID}/messages`]: () =>
        sse([START, ['title', { title: 'Название от модели' }], ['done', { status: 'complete' }]]),
    });
    renderApp(`/chat/${ID}`);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    const history = within(screen.getByRole('complementary', { name: t.history }));
    expect(history.queryByRole('link')).not.toBeInTheDocument();

    await user.type(field(), 'Первый вопрос{Enter}');
    expect(await history.findByRole('link')).toBeInTheDocument();
  });

  it('достигнув предела числа файлов, интерфейс новую загрузку не начинает', async () => {
    const { server, user } = setupChat([]);
    let count = 0;
    server.on(`POST /api/dialogs/${ID}/attachments`, () => ok({ ...PDF, id: `f-${(count += 1)}` }, 201));
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    const files = Array.from({ length: 11 }, (_, index) => new File(['%PDF-'], `файл-${index + 1}.pdf`));
    await user.upload(fileInput(), files);
    expect(screen.getByRole('alert')).toHaveTextContent('К одному сообщению можно прикрепить не больше 10 файлов');
    await waitFor(() => expect(server.callsTo(`POST /api/dialogs/${ID}/attachments`)).toHaveLength(10));
    expect(screen.queryByText('файл-11.pdf')).not.toBeInTheDocument();

    await user.upload(fileInput(), new File(['%PDF-'], 'ещё один.pdf'));
    expect(screen.getByRole('alert')).toHaveTextContent('К одному сообщению можно прикрепить не больше 10 файлов');
    expect(server.callsTo(`POST /api/dialogs/${ID}/attachments`)).toHaveLength(10);
  });

  it('тип и размер проверяются в браузере до загрузки', async () => {
    const { server, user } = setupChat([]);
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    await user.upload(fileInput(), new File(['x'], 'макет.dwg'));
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Файл „макет.dwg“ не подходит. Можно прикрепить изображения JPG и PNG, PDF, DOCX, TXT, MD, аудио- и видеозаписи',
    );

    const big = new File(['x'], 'скан.pdf');
    Object.defineProperty(big, 'size', { value: 20971521 });
    await user.upload(fileInput(), big);
    expect(screen.getByRole('alert')).toHaveTextContent('Файл „скан.pdf“ больше 20 МБ. Уменьшите его или разделите на части');
    expect(server.callsTo(`POST /api/dialogs/${ID}/attachments`)).toHaveLength(0);
  });

  it.each([
    [415, 'unsupported_file_type', undefined, 'Файл „отчёт.pdf“ не подходит. Можно прикрепить изображения JPG и PNG, PDF, DOCX, TXT, MD, аудио- и видеозаписи'],
    [413, 'file_too_large', { max_bytes: 10485760 }, 'Файл „отчёт.pdf“ больше 10 МБ. Уменьшите его или разделите на части'],
    [422, 'too_many_pages', { max_pages: 150 }, 'В файле „отчёт.pdf“ больше 150 страниц. Разделите его на части'],
    [422, 'too_many_images', { max_images: 8 }, 'В файле „отчёт.pdf“ больше 8 страниц без текста, а модель видит не больше 8 изображений за раз. Откройте его в разделе „Документы“ или добавьте в базу знаний'],
    [422, 'file_unreadable', undefined, 'Не удалось прочитать файл „отчёт.pdf“. Проверьте, что он открывается и не защищён паролем'],
  ])('отказ сервера %s %s — текст под панелью запроса, вложение убрано', async (status, code, details, text) => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/attachments`, () => fail(status, code, { details }));
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    await user.upload(fileInput(), new File(['%PDF-'], 'отчёт.pdf'));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.queryByRole('button', { name: t.files.remove('отчёт.pdf') })).not.toBeInTheDocument();
  });

  it('загрузка оборвалась — «Повторить» у вложения; пока файл загружается, отправка недоступна', async () => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/attachments`, () => {
      throw new TypeError('Failed to fetch');
    });
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    await user.upload(fileInput(), new File(['%PDF-'], 'договор.pdf'));
    const retry = await screen.findByRole('button', { name: texts.common.retry });

    let finish: (() => void) | undefined;
    server.on(`POST /api/dialogs/${ID}/attachments`, () => new Promise((resolve) => (finish = () => resolve(ok(PDF, 201)))));
    await user.click(retry);
    await user.type(field(), 'Вопрос{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.composer.uploading);

    finish?.();
    expect(await screen.findByText('· 3 стр.')).toBeInTheDocument();

    server.on(`DELETE /api/dialogs/${ID}/attachments/f-1`, () => ok());
    await user.click(screen.getByRole('button', { name: 'Убрать файл договор.pdf' }));
    expect(screen.queryByText('договор.pdf')).not.toBeInTheDocument();
    await waitFor(() => expect(server.callsTo(`DELETE /api/dialogs/${ID}/attachments/f-1`)).toHaveLength(1));
  });

  it('картинка из буфера обмена прикрепляется вставкой; предел изображений проверяется до отправки', async () => {
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/attachments`, () => ok({ ...PNG, id: 'f-9', image_count: 9 }, 201));
    await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

    fireEvent.paste(field(), { clipboardData: { files: [new File(['png'], 'image.png', { type: 'image/png' })] } });
    await waitFor(() => expect(screen.getByRole('main').querySelector('img')).not.toBeNull());

    await user.click(screen.getByRole('button', { name: t.composer.send }));
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Модель видит не больше 8 изображений и страниц сканов за раз. Уберите лишние вложения',
    );
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)).toHaveLength(0);
  });

  describe('записи', () => {
    const MEDIA = `POST /api/dialogs/${ID}/attachments/media`;
    const record = (id: string, name: string, status: 'queued' | 'processing' | 'ready' | 'error', patch: Partial<Attachment> = {}): Attachment => ({
      id,
      file_name: name,
      media_type: 'audio/mpeg',
      page_count: null,
      image_count: 0,
      duration_seconds: status === 'ready' ? 742 : null,
      transcription: { status, error_code: null },
      created_at: '',
      ...patch,
    });
    const mp3 = (name: string) => new File(['ID3'], name, { type: 'audio/mpeg' });
    const chip = (name: string) => screen.getByText(name).closest('span[class*="media"]') as HTMLElement;

    async function ready() {
      const setup = setupChat([]);
      await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());
      return setup;
    }

    it('загружаются строго по одной своим маршрутом; ход виден; расшифровка — опросом; до готовности отправки нет', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const { server, user } = await ready();
        const finish: (() => void)[] = [];
        server.on(MEDIA, (_body, url) => new Promise((resolve) => finish.push(() => resolve(ok(record(`f-${finish.length}`, url.searchParams.get('file_name') ?? '', 'queued'), 201)))));
        server.on(`POST /api/dialogs/${ID}/attachments`, () => ok({ ...record('f-9', 'договор.pdf', 'ready'), media_type: 'application/pdf', page_count: 3, transcription: null, duration_seconds: null }, 201));

        await user.upload(fileInput(), [mp3('совещание.mp3'), mp3('планёрка.mp3'), new File(['%PDF-'], 'договор.pdf')]);
        // Вторая запись ждёт; документ загружается как прежде, не дожидаясь записей.
        await waitFor(() => expect(server.uploads).toHaveLength(1));
        expect(server.uploads[0]?.path).toBe(`/api/dialogs/${ID}/attachments/media?file_name=%D1%81%D0%BE%D0%B2%D0%B5%D1%89%D0%B0%D0%BD%D0%B8%D0%B5.mp3`);
        const upload = server.callsTo(MEDIA)[0];
        expect(upload?.body).toBeInstanceOf(File);
        expect(upload?.headers['X-Portal-Csrf']).toBe('1');
        expect(chip('планёрка.mp3')).toHaveTextContent('Ждёт загрузки');
        expect(await screen.findByText('· 3 стр.')).toBeInTheDocument();

        act(() => server.uploads[0]?.progress(40, 100));
        expect(chip('совещание.mp3')).toHaveTextContent('Загружается… 40 %');
        await user.type(field(), 'Что решили?{Enter}');
        expect(screen.getByRole('alert')).toHaveTextContent(t.composer.uploading);

        finish[0]?.();
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Ждёт расшифровки'));
        await waitFor(() => expect(server.uploads).toHaveLength(2));
        finish[1]?.();
        await waitFor(() => expect(chip('планёрка.mp3')).toHaveTextContent('Ждёт расшифровки'));
        // Файлы загрузились: просьба подождать под панелью больше не нужна.
        await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());

        await user.click(screen.getByRole('button', { name: t.composer.send }));
        expect(screen.getByRole('alert')).toHaveTextContent(
          'Запись ещё расшифровывается — отправить сообщение можно будет, когда она будет готова',
        );
        expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)).toHaveLength(0);

        // Состояние перезапрашивается каждые 5 секунд, пока запись не готова.
        server.on(`GET /api/dialogs/${ID}/attachments/f-1`, () => ok(record('f-1', 'совещание.mp3', 'processing')));
        server.on(`GET /api/dialogs/${ID}/attachments/f-2`, () => ok(record('f-2', 'планёрка.mp3', 'ready')));
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Расшифровывается…'));
        expect(chip('планёрка.mp3')).toHaveTextContent('Расшифровано · 12 мин');
        expect(within(chip('планёрка.mp3')).getByRole('status')).toHaveTextContent('Запись расшифрована');

        server.on(`GET /api/dialogs/${ID}/attachments/f-1`, () => ok(record('f-1', 'совещание.mp3', 'ready')));
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Расшифровано · 12 мин'));
        await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
        // Готовые записи больше не опрашиваются.
        const polls = server.calls.filter((call) => call.method === 'GET' && call.path.includes('/attachments/')).length;
        await vi.advanceTimersByTimeAsync(11000);
        expect(server.calls.filter((call) => call.method === 'GET' && call.path.includes('/attachments/'))).toHaveLength(polls);

        server.on(`POST /api/dialogs/${ID}/messages`, () => sse([START, ['done', { status: 'complete' }]]));
        await user.click(screen.getByRole('button', { name: t.composer.send }));
        await waitFor(() =>
          expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({ attachment_ids: ['f-1', 'f-2', 'f-9'] }),
        );
      } finally {
        vi.useRealTimers();
      }
    });

    it.each([
      [415, 'unsupported_file_type', undefined, 'Файл „запись.mp3“ не подходит. Подойдут MP3, WAV, M4A, OGG, FLAC, MP4, MKV, WebM и MOV. Если формат верный — сохраните запись заново в MP3', false],
      [413, 'file_too_large', { max_bytes: 2147483648 }, 'Файл „запись.mp3“ больше 2 ГБ. Сожмите запись или разделите её на части', false],
      [507, 'media_quota_exceeded', { reason: 'free_space' }, 'На портале закончилось место для записей. Сообщите администратору', false],
      [507, 'media_quota_exceeded', { reason: 'user' }, 'Вы заняли всё место, отведённое вам для записей. Удалите ненужные записи из базы знаний и повторите', false],
      [429, 'too_many_uploads', undefined, 'Сейчас загружается слишком много записей. Повторите через несколько минут', true],
      [408, 'upload_timeout', undefined, 'Сообщение сервера: upload_timeout.', true],
    ] as const)('отказ загрузки %s %s — причина у вложения, отправка недоступна', async (status, code, details, text, retryable) => {
      const { server, user } = await ready();
      server.on(MEDIA, () => fail(status, code, { details }));

      await user.upload(fileInput(), mp3('запись.mp3'));
      await waitFor(() => expect(chip('запись.mp3')).toHaveTextContent(text));
      expect(within(chip('запись.mp3')).queryByRole('button', { name: texts.common.retry }) !== null).toBe(retryable);
      // Сам интерфейс загрузку не повторяет.
      expect(server.callsTo(MEDIA)).toHaveLength(1);

      await user.type(field(), 'Вопрос{Enter}');
      expect(screen.getByRole('alert')).toHaveTextContent('Уберите запись с ошибкой — её можно прикрепить заново');
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)).toHaveLength(0);
    });

    it('обрыв связи — «Повторить» начинает загрузку заново; отказ одной записи очередь не останавливает', async () => {
      const { server, user } = await ready();
      server.on(MEDIA, (_body, url) => {
        if (url.searchParams.get('file_name') === 'первая.mp3') {
          throw new TypeError('network error');
        }
        return ok(record('f-2', 'вторая.mp3', 'ready'), 201);
      });

      await user.upload(fileInput(), [mp3('первая.mp3'), mp3('вторая.mp3')]);
      await waitFor(() => expect(chip('вторая.mp3')).toHaveTextContent('Расшифровано · 12 мин'));
      expect(chip('первая.mp3')).toHaveTextContent('Загрузка оборвалась');

      server.on(MEDIA, () => ok(record('f-1', 'первая.mp3', 'ready'), 201));
      await user.click(within(chip('первая.mp3')).getByRole('button', { name: texts.common.retry }));
      await waitFor(() => expect(chip('первая.mp3')).toHaveTextContent('Расшифровано · 12 мин'));
      expect(server.callsTo(MEDIA)).toHaveLength(3);
    });

    it('крестик отменяет идущую загрузку и убирает ждущую из очереди; удаление загруженной снимает её на сервере', async () => {
      const { server, user } = await ready();
      server.on(MEDIA, (_body, url) =>
        url.searchParams.get('file_name') === 'третья.mp3' ? ok(record('f-3', 'третья.mp3', 'queued'), 201) : new Promise(() => undefined),
      );
      server.on(`DELETE /api/dialogs/${ID}/attachments/f-3`, () => ok());

      await user.upload(fileInput(), [mp3('первая.mp3'), mp3('вторая.mp3'), mp3('третья.mp3')]);
      await waitFor(() => expect(server.uploads).toHaveLength(1));
      await user.click(screen.getByRole('button', { name: 'Убрать файл вторая.mp3' }));
      await user.click(screen.getByRole('button', { name: 'Убрать файл первая.mp3' }));
      expect(server.uploads[0]?.aborted).toBe(true);

      // Очередь идёт дальше: вторая не загружалась вовсе.
      await waitFor(() => expect(chip('третья.mp3')).toHaveTextContent('Ждёт расшифровки'));
      expect(server.uploads.map((upload) => upload.path)).toEqual([
        expect.stringContaining(encodeURIComponent('первая.mp3')),
        expect.stringContaining(encodeURIComponent('третья.mp3')),
      ]);
      expect(screen.queryByText('первая.mp3')).not.toBeInTheDocument();

      await user.click(screen.getByRole('button', { name: 'Убрать файл третья.mp3' }));
      await waitFor(() => expect(server.callsTo(`DELETE /api/dialogs/${ID}/attachments/f-3`)).toHaveLength(1));
    });

    it('ошибка расшифровки: причина у вложения, повтора нет; запись длиннее предела — совет про базу знаний', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const { server, user } = await ready();
        server.on(MEDIA, () => ok(record('f-1', 'совещание.mp3', 'queued'), 201));
        server.on(`GET /api/dialogs/${ID}/attachments/f-1`, () =>
          ok(record('f-1', 'совещание.mp3', 'error', { transcription: { status: 'error', error_code: 'media_too_long' } })),
        );

        await user.upload(fileInput(), mp3('совещание.mp3'));
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Ждёт расшифровки'));
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() =>
          expect(chip('совещание.mp3')).toHaveTextContent(
            'Запись „совещание.mp3“ длиннее 20 минут — для вложения это слишком много. Добавьте её в базу знаний и задайте вопрос, отметив базу под полем ввода',
          ),
        );
        expect(within(chip('совещание.mp3')).queryByRole('button', { name: texts.common.retry })).not.toBeInTheDocument();
        // Запись с ошибкой больше не опрашивается.
        await vi.advanceTimersByTimeAsync(11000);
        expect(server.callsTo(`GET /api/dialogs/${ID}/attachments/f-1`)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('опрос расшифровки ответил отказом сервера: это не ошибка расшифровки — следующий опрос показывает готовую запись', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const { server, user } = await ready();
        server.on(MEDIA, () => ok(record('f-1', 'совещание.mp3', 'queued'), 201));
        server.on(`GET /api/dialogs/${ID}/attachments/f-1`, () => fail(503, 'service_unavailable'));

        await user.upload(fileInput(), mp3('совещание.mp3'));
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Ждёт расшифровки'));
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() => expect(server.callsTo(`GET /api/dialogs/${ID}/attachments/f-1`)).toHaveLength(1));
        expect(chip('совещание.mp3')).toHaveTextContent('Ждёт расшифровки');

        server.on(`GET /api/dialogs/${ID}/attachments/f-1`, () => ok(record('f-1', 'совещание.mp3', 'ready')));
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Расшифровано · 12 мин'));
      } finally {
        vi.useRealTimers();
      }
    });

    it('вложения больше нет на сервере — запись с ошибкой, опрос прекращается', async () => {
      vi.useFakeTimers({ shouldAdvanceTime: true });
      try {
        const { server, user } = await ready();
        server.on(MEDIA, () => ok(record('f-1', 'совещание.mp3', 'queued'), 201));
        server.on(`GET /api/dialogs/${ID}/attachments/f-1`, () => fail(404, 'not_found'));

        await user.upload(fileInput(), mp3('совещание.mp3'));
        await waitFor(() => expect(chip('совещание.mp3')).toHaveTextContent('Ждёт расшифровки'));
        await vi.advanceTimersByTimeAsync(5100);
        await waitFor(() => expect(chip('совещание.mp3')).not.toHaveTextContent('Ждёт расшифровки'));
        await vi.advanceTimersByTimeAsync(11000);
        expect(server.callsTo(`GET /api/dialogs/${ID}/attachments/f-1`)).toHaveLength(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('чат удалён во время загрузки записи: загрузка отменяется, ждавшая запись не отправляется', async () => {
      const { server, user } = setupChat([QUESTION]);
      await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());
      server.on(MEDIA, () => new Promise(() => undefined));
      server.on(`DELETE /api/dialogs/${ID}`, () => ok());
      await user.upload(fileInput(), [mp3('совещание.mp3'), mp3('планёрка.mp3')]);
      await waitFor(() => expect(server.uploads).toHaveLength(1));

      const history = within(screen.getByRole('complementary', { name: t.history }));
      await user.click(await history.findByRole('button', { name: 'Действия: Аренда участка под ЛЭП' }));
      await user.click(await screen.findByText(t.menu.remove));
      await user.click(await screen.findByRole('button', { name: t.remove.action }));
      expect(await screen.findByText(t.remove.done)).toBeInTheDocument();

      expect(server.uploads[0]?.aborted).toBe(true);
      expect(server.uploads).toHaveLength(1);
    });

    it('очередь одна на чат и базу знаний: запись из окна добавления ждёт, пока загружается запись чата', async () => {
      vi.stubGlobal(
        'DataTransfer',
        class {
          items = { add: () => undefined };
          files = null;
        },
      );
      const { server, user } = await ready();
      let finish: () => void = () => undefined;
      server.on(MEDIA, () => new Promise((resolve) => (finish = () => resolve(ok(record('f-1', 'совещание.mp3', 'ready'), 201)))));
      server.on('GET /api/kb/tags', () => ok({ items: [] }));
      server.on('POST /api/kb/documents/media', () => new Promise(() => undefined));
      await user.upload(fileInput(), mp3('совещание.mp3'));
      await waitFor(() => expect(server.uploads).toHaveLength(1));

      await user.click(screen.getByRole('link', { name: texts.nav.knowledge }));
      await waitFor(() => expect(screen.getAllByRole('button', { name: texts.kb.add })[0]).toBeEnabled());
      await user.click(screen.getAllByRole('button', { name: texts.kb.add })[0] as HTMLElement);
      const modal = within(await screen.findByRole('dialog'));
      fireEvent.change(document.querySelector('[role="dialog"] input[type="file"]') as HTMLInputElement, {
        target: { files: [mp3('планёрка.mp3')] },
      });
      await user.click(modal.getByRole('button', { name: texts.kb.upload.submit }));
      expect(await modal.findByText(texts.kb.upload.waiting)).toBeInTheDocument();
      expect(server.uploads).toHaveLength(1);

      finish();
      await waitFor(() => expect(server.uploads).toHaveLength(2));
      expect(server.uploads[1]?.path).toContain('/api/kb/documents/media?');
    });

    it('запись убрана до того, как создан новый чат: загрузка не начинается', async () => {
      const user = userEvent.setup({ applyAccept: false });
      let create: () => void = () => undefined;
      const server = mockApi({
        'GET /api/auth/session': () => ok(session('ready')),
        'POST /api/dialogs': () => new Promise((resolve) => (create = () => resolve(ok(dialog({ title: null }), 201)))),
        [MEDIA]: () => ok(record('f-1', 'совещание.mp3', 'queued'), 201),
        [`POST /api/dialogs/${ID}/attachments`]: () => ok({ ...record('f-9', 'договор.pdf', 'ready'), media_type: 'application/pdf', page_count: 3, transcription: null, duration_seconds: null }, 201),
      });
      renderApp('/chat');
      await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());

      await user.upload(fileInput(), [mp3('совещание.mp3'), new File(['%PDF-'], 'договор.pdf')]);
      await user.click(await screen.findByRole('button', { name: 'Убрать файл совещание.mp3' }));
      create();

      // Оставшийся файл загружается, убранная запись — нет.
      expect(await screen.findByText('· 3 стр.')).toBeInTheDocument();
      expect(screen.queryByText('совещание.mp3')).not.toBeInTheDocument();
      expect(server.uploads).toHaveLength(0);
      expect(server.callsTo(MEDIA)).toHaveLength(0);
    });

    it('размер записи проверяется до загрузки; при выключенном приёме запись — обычное «не подходит»', async () => {
      const { server, user } = await ready();
      const big = mp3('совещание.mp4');
      Object.defineProperty(big, 'size', { value: 2147483649 });
      await user.upload(fileInput(), big);
      expect(screen.getByRole('alert')).toHaveTextContent('Файл „совещание.mp4“ больше 2 ГБ. Сожмите запись или разделите её на части');
      expect(fileInput().accept).toContain('.mp3');
      await user.hover(screen.getByRole('button', { name: t.composer.attach }));
      expect(await screen.findByText(/Аудио- и видеозаписи с русской речью — до 20 минут\. Не больше 10 файлов/)).toBeInTheDocument();
      expect(server.uploads).toHaveLength(0);
    });

    it('приём записей выключен: запись не предлагается и получает обычное «не подходит»', async () => {
      const server = mockApi({
        'GET /api/auth/session': () => ok(session('ready')),
        'GET /api/config': () => ok({ ...CONFIG, media: { ...CONFIG.media, enabled: false } }),
        [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
        [`GET /api/dialogs/${ID}`]: () => ok(dialog()),
      });
      renderApp(`/chat/${ID}`);
      const user = userEvent.setup({ applyAccept: false });
      await waitFor(() => expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled());
      await waitFor(() => expect(fileInput().accept).toContain('.pdf'));
      expect(fileInput().accept).not.toContain('.mp3');

      await user.upload(fileInput(), mp3('совещание.mp3'));
      expect(screen.getByRole('alert')).toHaveTextContent(
        'Файл „совещание.mp3“ не подходит. Можно прикрепить изображения JPG и PNG, PDF, DOCX, TXT и MD',
      );
      expect(screen.getByRole('alert')).not.toHaveTextContent('записи');
      expect(server.uploads).toHaveLength(0);
    });

    it('запись в отправленном вопросе: имя и длительность, не нажимается', async () => {
      setupChat([message({ id: 'u-1', role: 'user', content: 'Что решили?', attachments: [record('f-1', 'совещание.mp3', 'ready')] })]);
      const name = await screen.findByText('совещание.mp3');
      expect(name.parentElement).toHaveTextContent('совещание.mp3 · 12 мин');
      expect(name.closest('button')).toBeNull();
      expect(screen.getByRole('main').querySelector('img')).toBeNull();
    });
  });

  it('изображение в сообщении открывается в окне', async () => {
    const { user } = setupChat([message({ id: 'u-1', role: 'user', content: 'Что на скане?', attachments: [PNG, PDF] })]);

    await user.click(await screen.findByRole('button', { name: /скан\.png/ }));
    const modal = await screen.findByRole('dialog');
    expect(within(modal).getByRole('img', { name: 'скан.png' })).toHaveAttribute('src', `/api/dialogs/${ID}/attachments/f-2/file`);
  });
});

describe('чат: история', () => {
  it('группы по дате, текущий чат отмечен, «Показать ещё» подгружает', async () => {
    const day = 24 * 60 * 60 * 1000;
    const old = dialog({ id: 'd-2', title: 'Запрос по ЕГРН', updated_at: new Date(Date.now() - 30 * day).toISOString() });
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'GET /api/dialogs': (_body, url) =>
        url.searchParams.get('cursor')
          ? ok({ items: [dialog({ id: 'd-3', title: null, updated_at: old.updated_at })], next_cursor: null })
          : ok({ items: [dialog(), old], next_cursor: 'next' }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
    });
    renderApp(`/chat/${ID}`);

    const history = within(await screen.findByRole('complementary', { name: t.history }));
    expect(await history.findByRole('heading', { name: t.groups.today })).toBeInTheDocument();
    expect(history.getByRole('heading', { name: t.groups.earlier })).toBeInTheDocument();
    expect(history.getByRole('link', { name: 'Аренда участка под ЛЭП' })).toHaveAttribute('aria-current', 'page');
    expect(new URL(`http://x${server.calls.find((call) => call.path === '/api/dialogs')?.path}`).pathname).toBe('/api/dialogs');

    await userEvent.setup().click(history.getByRole('button', { name: t.showMore }));
    // Чат без названия показан как «Новый чат».
    expect(await history.findByRole('link', { name: t.newChat })).toBeInTheDocument();
    expect(history.queryByRole('button', { name: t.showMore })).not.toBeInTheDocument();
  });

  it('переименование на месте: пустое название не сохраняется, Enter сохраняет, Esc отменяет', async () => {
    const { server, user } = setupChat([QUESTION]);
    server.on(`PATCH /api/dialogs/${ID}`, (body) => ok(dialog({ title: (body as { title: string }).title })));
    await screen.findByRole('heading', { level: 1, name: 'Аренда участка под ЛЭП' });

    const openRename = async () => {
      await user.click(within(screen.getByRole('main')).getAllByRole('button', { name: 'Действия: Аренда участка под ЛЭП' }).at(-1) as HTMLElement);
      await user.click(await screen.findByText(t.menu.rename));
      return screen.findByLabelText(t.titleLabel);
    };

    let input = await openRename();
    await user.clear(input);
    await user.keyboard('{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.enterTitle);
    await user.type(input, 'я'.repeat(201));
    await user.keyboard('{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.titleTooLong);
    await user.keyboard('{Escape}');
    expect(server.callsTo(`PATCH /api/dialogs/${ID}`)).toHaveLength(0);
    expect(screen.getByRole('heading', { level: 1, name: 'Аренда участка под ЛЭП' })).toBeInTheDocument();

    input = await openRename();
    await user.clear(input);
    await user.type(input, 'Аренда под ЛЭП{Enter}');
    expect(await screen.findByRole('heading', { level: 1, name: 'Аренда под ЛЭП' })).toBeInTheDocument();
    expect(server.callsTo(`PATCH /api/dialogs/${ID}`)[0]?.body).toEqual({ title: 'Аренда под ЛЭП' });
    expect(screen.getByRole('link', { name: 'Аренда под ЛЭП' })).toBeInTheDocument();
  });

  it('удаление: подтверждение, «Чат удалён», открывается новый чат; сбой оставляет чат в истории', async () => {
    const { server, user } = setupChat([QUESTION]);
    server.on(`DELETE /api/dialogs/${ID}`, () => fail(500, 'internal_error'));
    const history = within(await screen.findByRole('complementary', { name: t.history }));

    const openRemove = async () => {
      await user.click(await history.findByRole('button', { name: 'Действия: Аренда участка под ЛЭП' }));
      await user.click(await screen.findByText(t.menu.remove));
    };

    await openRemove();
    expect(await screen.findByText('Удалить чат „Аренда участка под ЛЭП“?')).toBeInTheDocument();
    expect(screen.getByText(t.remove.body)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: texts.common.cancel })).toHaveFocus());
    await user.click(screen.getByRole('button', { name: t.remove.action }));
    expect(await screen.findByText(t.remove.failed)).toBeInTheDocument();
    expect(history.getByRole('link', { name: 'Аренда участка под ЛЭП' })).toBeInTheDocument();

    server.on(`DELETE /api/dialogs/${ID}`, () => ok());
    await openRemove();
    await user.click(await screen.findByRole('button', { name: t.remove.action }));
    expect(await screen.findByText(t.remove.done)).toBeInTheDocument();
    await waitFor(() => expect(path()).toBe('/chat'));
    expect(history.queryByRole('link', { name: 'Аренда участка под ЛЭП' })).not.toBeInTheDocument();
  });

  it('чужой или удалённый чат по прямому адресу — «Чат не найден»', async () => {
    mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'GET /api/dialogs/d-x/messages': () => fail(404, 'not_found'),
      'GET /api/dialogs/d-x': () => fail(404, 'not_found'),
    });
    renderApp('/chat/d-x');

    expect(await screen.findByRole('heading', { level: 2, name: t.notFound.title })).toBeInTheDocument();
    expect(screen.getByText(t.notFound.text)).toBeInTheDocument();
    expect(screen.queryByPlaceholderText(t.composer.placeholder)).not.toBeInTheDocument();
    await userEvent.setup().click(within(screen.getByRole('main')).getAllByRole('button', { name: t.newChat }).at(-1) as HTMLElement);
    expect(path()).toBe('/chat');
  });
});

describe('чат: база знаний', () => {
  const source = (n: number, patch: Partial<Source> = {}): Source => ({
    n,
    document_id: `doc-${n}`,
    document_title: `Договор аренды № 14-А, часть ${n}.pdf`,
    scope: 'shared',
    page: n + 1,
    timecode_seconds: null,
    image_id: null,
    fragment_id: `frag-${n}`,
    quote: `…сроком на 49 (сорок девять) лет, пункт ${n}…`,
    ...patch,
  });

  const KB_DOC: KbDocument = {
    id: 'doc-1',
    title: 'Договор аренды № 14-А.pdf',
    scope: 'shared',
    kind: 'document',
    is_cogis: false,
    tags: [],
    author: { full_name: 'Петров Алексей Сергеевич', is_me: false },
    created_at: '2026-09-12T09:00:00Z',
    page_count: 14,
    duration_seconds: null,
    status: 'ready',
    error_code: null,
    progress: null,
    images_truncated: false,
    reprocess_error: null,
    reprocessing: false,
    can_delete: false,
  };

  /** Текст страницы по контракту §8.3: отрезки — пары «текст, подсвечен ли». */
  const pageText = (page: number | null, parts: [string, boolean][], patch: Partial<DocumentText> = {}): DocumentText => ({
    page,
    page_count: page === null ? null : 14,
    recognized: false,
    segments: parts.map(([text, highlight]) => ({ text, highlight, start_seconds: null })),
    timecode_seconds: null,
    image: null,
    ...patch,
  });

  const textUrls = () => vi.mocked(fetch).mock.calls.map(([input]) => String(input)).filter((url) => url.includes('/text'));

  it('выбор баз и тегов: набор уходит в запросе, базы запоминаются, теги — нет; ноль найденного — совет про теги', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    server.on('GET /api/kb/tags', () => ok({ items: ['аренда', 'ЛЭП'] }));
    expect(await screen.findByText(/включите базу знаний под полем ввода/)).toBeInTheDocument();

    const button = screen.getByRole('button', { name: 'База знаний: не используется' });
    expect(button).toHaveAttribute('aria-expanded', 'false');
    await user.click(button);
    const panel = await screen.findByRole('dialog', { name: 'База знаний' });
    // Фокус — на первой отметке; пока не отмечена ни одна база, поле тегов недоступно.
    await waitFor(() => expect(within(panel).getByRole('checkbox', { name: /^Общая —/ })).toHaveFocus());
    expect(within(panel).getByRole('textbox')).toBeDisabled();
    expect(within(panel).getByText('Сначала отметьте, где искать')).toBeInTheDocument();

    // Отмечается любое сочетание; порядок в подписи и в запросе — как в панели, а не как нажимали.
    await user.click(within(panel).getByRole('checkbox', { name: /^Министерства —/ }));
    await user.click(within(panel).getByRole('checkbox', { name: /^Общая —/ }));
    expect(window.localStorage.getItem('portal.chat.collections')).toBe('shared,ministries');

    await user.click(within(panel).getByRole('textbox'));
    await user.click(await screen.findByText('аренда'));
    expect(String(vi.mocked(fetch).mock.calls.map(([input]) => String(input)).find((url) => url.includes('/api/kb/tags')))).toContain(
      'scope=shared&scope=ministries',
    );
    expect(await screen.findByRole('button', { name: 'База знаний: Общая, Министерства, теги: 1' })).toHaveTextContent(
      'База знаний: Общая, Министерства · теги: 1',
    );

    // Esc закрывает панель и возвращает фокус на кнопку.
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'База знаний' })).not.toBeInTheDocument());
    expect(screen.getByRole('button', { name: /^База знаний:/ })).toHaveFocus();

    await user.type(field(), 'Какой срок аренды?{Enter}');
    live.push(START, ['search_started', {}]);
    expect((await screen.findAllByText(/Ищу в базе знаний/)).length).toBeGreaterThan(0);
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({
      collections: ['shared', 'ministries'],
      tags: ['аренда'],
    });
    // Теги живут, пока открыта вкладка: в браузере не сохраняются.
    expect(JSON.stringify({ ...window.localStorage })).not.toContain('аренда');

    // Ноль найденного виден сразу по событию `sources`; у вопроса с тегами — совет убрать их.
    live.push(['sources', { sources: [] }]);
    expect(await screen.findByText(t.sources.nothingFound.withTags)).toBeInTheDocument();
    live.push(['delta', { text: 'В базе знаний об этом ничего нет.' }], ['done', { status: 'complete' }]);
    live.close();
    await screen.findByRole('button', { name: t.composer.send });
    expect(screen.queryByText(t.sources.notCited)).not.toBeInTheDocument();
  });

  it('выбор баз: прежний запомненный выбор переводится в набор; без тегов ноль найденного — текст про пустую базу', async () => {
    window.localStorage.setItem('portal.chat.knowledge', 'shared_and_personal');
    const { server, user } = setupChat([]);
    server.on(`POST /api/dialogs/${ID}/messages`, () =>
      sse([START, ['search_started', {}], ['sources', { sources: [] }], ['delta', { text: 'Ничего.' }], ['done', { status: 'complete' }]]),
    );

    expect(await screen.findByRole('button', { name: 'База знаний: Общая, Моя' })).toBeInTheDocument();
    await user.type(field(), 'Вопрос{Enter}');
    expect(await screen.findByText(t.sources.nothingFound.withoutTags)).toBeInTheDocument();
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({ collections: ['shared', 'personal'], tags: [] });
  });

  it('теги: предел числа из конфигурации — новый тег не принимается; подсказки не загрузились — заметка, базы выбираются', async () => {
    window.localStorage.setItem('portal.chat.collections', 'shared');
    const { server, user } = setupChat([]);
    server.on('GET /api/config', () => ok({ ...CONFIG, kb: { ...CONFIG.kb, tags: { ...CONFIG.kb.tags, max_selected: 1 } } }));
    server.on('GET /api/kb/tags', () => ok({ items: ['аренда', 'ЛЭП'] }));
    await user.click(await screen.findByRole('button', { name: 'База знаний: Общая' }));
    const panel = await screen.findByRole('dialog', { name: 'База знаний' });

    await user.click(within(panel).getByRole('textbox'));
    await user.click(await screen.findByText('ЛЭП'));
    expect(await within(panel).findByText('Можно выбрать не больше 1 тега')).toBeInTheDocument();
    // Предел достигнут: подсказок больше нет, запрос за ними не идёт.
    const requests = server.callsTo('GET /api/kb/tags').length;
    await user.type(within(panel).getByRole('textbox'), 'ар');
    expect(screen.queryByText('аренда')).not.toBeInTheDocument();
    expect(server.callsTo('GET /api/kb/tags')).toHaveLength(requests);

    // Тег убран — строка о пределе исчезает; сбой подсказок базам не мешает.
    server.on('GET /api/kb/tags', () => fail(503, 'service_unavailable'));
    await user.clear(within(panel).getByRole('textbox'));
    await user.keyboard('{Backspace}{Backspace}');
    await waitFor(() => expect(within(panel).queryByText('Можно выбрать не больше 1 тега')).not.toBeInTheDocument());
    await user.type(within(panel).getByRole('textbox'), 'а');
    expect(await within(panel).findByText('Не удалось загрузить теги')).toBeInTheDocument();
    expect(screen.queryByText('Такого тега нет')).not.toBeInTheDocument();
    await user.click(within(panel).getByRole('checkbox', { name: /^Моя —/ }));
    expect(window.localStorage.getItem('portal.chat.collections')).toBe('shared,personal');
  });

  it('по завершении ответа — кнопки-сноски и карточки только упомянутых источников; сноска в коде остаётся текстом', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });

    await user.type(field(), 'Вопрос{Enter}');
    live.push(START, ['search_started', {}], ['sources', { sources: [source(1), source(2), source(3)] }]);
    expect((await screen.findAllByText(/Готовлю ответ/)).length).toBeGreaterThan(0);
    expect(screen.queryByText(t.sending)).not.toBeInTheDocument();
    live.push(['delta', { text: 'Срок — 49 лет [1]. В коде `coords[2]` не сноска. Нет источника [9].' }]);
    await screen.findByText(/Срок — 49 лет/);
    // Пока ответ идёт, карточек нет.
    expect(screen.queryByText(t.sources.title)).not.toBeInTheDocument();

    live.push(['done', { status: 'complete' }]);
    live.close();
    expect(await screen.findByRole('heading', { name: t.sources.title })).toBeInTheDocument();
    const mark = screen.getByRole('button', { name: 'Источник 1: Договор аренды № 14-А, часть 1.pdf, страница 2' });
    expect(mark).toHaveTextContent('[1]');
    expect(screen.queryByRole('button', { name: /Источник 2/ })).not.toBeInTheDocument();
    expect(screen.getByText('coords[2]').tagName).toBe('CODE');
    expect(screen.getByRole('main')).toHaveTextContent('Нет источника [9].');
    expect(screen.getAllByText(/сорок девять/)).toHaveLength(1);
  });

  it('кнопки-сноски стоят по правилу сервера, а не по разбору Markdown', async () => {
    const content = [
      'Абзац [1],',
      '    продолжение с отступом [2] — для сервера это код,',
      'и снова текст [3]. Одинокий ` апостроф [1].',
    ].join('\n');
    setupChat([QUESTION, message({ content, sources_found: 3, sources: [source(1), source(2), source(3)] })]);

    // Сервер засчитал бы [1] дважды и [3]; [2] в строке с отступом — нет, хотя Markdown показывает её текстом.
    expect(await screen.findAllByRole('button', { name: /^Источник 1:/ })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: /^Источник 3:/ })).toHaveLength(1);
    expect(screen.queryByRole('button', { name: /^Источник 2:/ })).not.toBeInTheDocument();
    expect(screen.getByRole('main')).toHaveTextContent('продолжение с отступом [2] — для сервера это код');
    expect(screen.getByRole('main').textContent).not.toMatch(/[\uE000\uE001]/);
  });

  it.each([
    [{ sources: null, sources_found: null }, null],
    [{ sources: [], sources_found: 4 }, t.sources.notCited],
    // В истории параметров вопроса нет: текст общий, без совета про теги.
    [{ sources: [], sources_found: 0 }, t.sources.nothingFound.fromHistory],
  ] as const)('что под ответом после перезагрузки: %o', async (patch, text) => {
    setupChat([QUESTION, message({ content: 'Ответ без ссылок', ...patch, sources: patch.sources ? [] : null })]);
    await screen.findByText('Ответ без ссылок');
    for (const line of [t.sources.notCited, ...Object.values(t.sources.nothingFound)]) {
      if (line === text) {
        expect(screen.getByText(line)).toBeInTheDocument();
      } else {
        expect(screen.queryByText(line)).not.toBeInTheDocument();
      }
    }
    expect(screen.queryByText(t.sources.title)).not.toBeInTheDocument();
  });

  it('карточка: страница, «Мой документ», документ без страниц; сноска и карточка подсвечивают друг друга', async () => {
    const { user } = setupChat([
      QUESTION,
      message({
        content: 'Первое [1], второе [2].',
        sources_found: 5,
        sources: [source(1), source(2, { page: null, scope: 'personal', document_title: 'Заметки.md' })],
      }),
    ]);

    const first = (await screen.findByText('Договор аренды № 14-А, часть 1.pdf')).closest('button') as HTMLElement;
    const second = screen.getByText('Заметки.md').closest('button') as HTMLElement;
    expect(first).toHaveTextContent('стр. 2');
    expect(second).toHaveTextContent('Мой документ');
    expect(second).not.toHaveTextContent('стр.');
    const mark = screen.getByRole('button', { name: 'Источник 2: Заметки.md' });

    const activeBefore = second.className;
    await user.hover(mark);
    expect(second.className).not.toBe(activeBefore);
    await user.unhover(mark);
    expect(second.className).toBe(activeBefore);
    const markBefore = mark.className;
    await user.hover(second);
    expect(mark.className).not.toBe(markBefore);
  });

  it('просмотр из карточки: страница цитаты, подсветка отрезков, сведения отдельным запросом, оригинал с #page', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Срок [1].', sources_found: 1, sources: [source(1)] })]);
    server.on('GET /api/kb/documents/doc-1/text', (_body, url) =>
      url.searchParams.get('page') === '3'
        ? ok(pageText(3, [['Третья страница', false]]))
        : ok(
            pageText(
              2,
              [
                ['1.1. Арендодатель предоставляет участок ', false],
                ['сроком на 49 (сорок девять) лет', true],
                [' с даты регистрации…', false],
              ],
              { recognized: true },
            ),
          ),
    );
    server.on('GET /api/kb/documents/doc-1', () => ok(KB_DOC));

    await user.click((await screen.findByText('Договор аренды № 14-А, часть 1.pdf')).closest('button') as HTMLElement);
    const quote = await screen.findByText('сроком на 49 (сорок девять) лет');
    expect(quote.tagName).toBe('MARK');
    expect(screen.getByText('Страница 2 из 14')).toBeInTheDocument();
    expect(screen.getByText(texts.viewer.recognized)).toBeInTheDocument();
    expect(await screen.findByText(/Общая база · добавил\(а\) Петров А\. С\./)).toBeInTheDocument();
    expect(screen.getByText(/12\.09\.2026 · 14 стр\./)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('href', '/api/kb/documents/doc-1/file#page=2');
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('target', '_blank');
    // Страница — из карточки, вместе с цитатой: после повторной обработки документ откроется на прежнем месте.
    expect(textUrls()[0]).toBe('/api/kb/documents/doc-1/text?page=2&fragment_id=frag-1');
    // Пока открыт просмотр, история скрыта, лента остаётся.
    expect(screen.queryByRole('complementary', { name: t.history })).not.toBeInTheDocument();
    expect(screen.getByText('Какой срок аренды?')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: texts.viewer.next }));
    expect(await screen.findByText('Третья страница')).toBeInTheDocument();
    expect(screen.queryByText(texts.viewer.recognized)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('href', '/api/kb/documents/doc-1/file#page=3');

    // При листании цитата передаётся вместе со страницей: вернувшись, видим её подсвеченной.
    expect(textUrls().at(-1)).toBe('/api/kb/documents/doc-1/text?page=3&fragment_id=frag-1');
    server.on('GET /api/kb/documents/doc-1/text', (_body, url) =>
      ok(pageText(2, [['сроком на 49 (сорок девять) лет', url.searchParams.get('fragment_id') === 'frag-1']], { recognized: true })),
    );
    await user.click(screen.getByRole('button', { name: texts.viewer.previous }));
    await waitFor(() => expect(textUrls().at(-1)).toBe('/api/kb/documents/doc-1/text?page=2&fragment_id=frag-1'));
    expect((await screen.findByText('сроком на 49 (сорок девять) лет')).tagName).toBe('MARK');
  });

  it('документ без страниц: навигации нет; сведения не получены — шапка с одним названием', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Срок [1].', sources_found: 1, sources: [source(1, { page: null })] })]);
    server.on('GET /api/kb/documents/doc-1/text', () =>
      ok(pageText(null, [['Весь текст документа', false]])),
    );
    server.on('GET /api/kb/documents/doc-1', () => fail(500, 'internal_error'));

    await user.click(await screen.findByRole('button', { name: /Источник 1/ }));
    expect(await screen.findByText('Весь текст документа')).toBeInTheDocument();
    expect(screen.queryByText(/Страница/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Общая база/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('href', '/api/kb/documents/doc-1/file');
    // У документа без страниц место не передаётся — только цитата.
    expect(textUrls()[0]).toBe('/api/kb/documents/doc-1/text?fragment_id=frag-1');
  });

  it.each([
    [404, 'not_found', texts.viewer.deleted],
    [409, 'document_not_ready', texts.viewer.notReady],
  ])('документ недоступен (%s %s): панель не открывается, уведомление', async (status, code, text) => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Срок [1].', sources_found: 1, sources: [source(1)] })]);
    server.on('GET /api/kb/documents/doc-1/text', () => fail(status, code));
    server.on('GET /api/kb/documents/doc-1', () => fail(status, code));

    await user.click(await screen.findByRole('button', { name: /Источник 1/ }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: texts.viewer.openOriginal })).not.toBeInTheDocument();
    // Карточка остаётся как была.
    expect(screen.getByText(/сорок девять/)).toBeInTheDocument();
  });

  it('пока просмотр открывается, на нажатой карточке — индикатор; сбой первого открытия — уведомление без панели', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Срок [1].', sources_found: 1, sources: [source(1)] })]);
    let respond: (() => void) | undefined;
    server.on('GET /api/kb/documents/doc-1', () => ok(KB_DOC));
    server.on('GET /api/kb/documents/doc-1/text', () => new Promise((resolve) => (respond = () => resolve(fail(500, 'internal_error')))));

    const card = (await screen.findByText('Договор аренды № 14-А, часть 1.pdf')).closest('button') as HTMLElement;
    expect(card.querySelector('[data-tid="Spinner__root"]')).toBeNull();
    await user.click(card);
    await waitFor(() => expect(card.querySelector('[data-tid="Spinner__root"]')).not.toBeNull());
    expect(screen.queryByRole('link', { name: texts.viewer.openOriginal })).not.toBeInTheDocument();

    respond?.();
    expect(await screen.findByText('Не удалось открыть документ. Попробуйте ещё раз')).toBeInTheDocument();
    await waitFor(() => expect(card.querySelector('[data-tid="Spinner__root"]')).toBeNull());
    expect(screen.queryByRole('link', { name: texts.viewer.openOriginal })).not.toBeInTheDocument();
    expect(screen.getByRole('complementary', { name: t.history })).toBeInTheDocument();
  });

  it('сбой при листании — заметка «Не удалось открыть страницу» с «Повторить»; удалённый документ закрывает панель', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Срок [1].', sources_found: 1, sources: [source(1)] })]);
    const second = ok(pageText(2, [['Вторая страница', false]]));
    server.on('GET /api/kb/documents/doc-1', () => ok(KB_DOC));
    server.on('GET /api/kb/documents/doc-1/text', (_body, url) => (url.searchParams.get('page') === '2' ? second : fail(500, 'internal_error')));

    await user.click(await screen.findByRole('button', { name: /Источник 1/ }));
    expect(await screen.findByText('Вторая страница')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: texts.viewer.next }));
    expect(await screen.findByText('Не удалось открыть страницу')).toBeInTheDocument();
    // Прежняя страница остаётся на месте.
    expect(screen.getByText('Вторая страница')).toBeInTheDocument();

    server.on('GET /api/kb/documents/doc-1/text', (_body, url) =>
      url.searchParams.get('page') === '3'
        ? ok(pageText(3, [['Третья страница', false]]))
        : second,
    );
    await user.click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByText('Третья страница')).toBeInTheDocument();
    expect(screen.queryByText('Не удалось открыть страницу')).not.toBeInTheDocument();

    server.on('GET /api/kb/documents/doc-1/text', (_body, url) => (url.searchParams.get('page') === '2' ? fail(404, 'not_found') : second));
    await user.click(screen.getByRole('button', { name: texts.viewer.previous }));
    expect(await screen.findByText(texts.viewer.deleted)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Третья страница')).not.toBeInTheDocument());
    expect(screen.getByRole('complementary', { name: t.history })).toBeInTheDocument();
  });

  it('источник-запись: момент в карточке и в имени сноски; просмотр открыт на моменте, звук — только по нажатию', async () => {
    const play = vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
    const record = source(2, { document_id: 'rec-1', document_title: 'Совещание 03.10.mp3', scope: 'ministries', page: null, timecode_seconds: 760 });
    const { server, user } = setupChat([QUESTION, message({ content: 'Продлеваем [2].', sources_found: 2, sources: [record] })]);
    const part = (page: number, patch: Partial<DocumentText>): DocumentText => ({
      page,
      page_count: 3,
      recognized: true,
      segments: [
        { text: 'По второму вопросу. ', highlight: false, start_seconds: 725 },
        { text: 'Предлагаю так: ', highlight: false, start_seconds: 760 },
        { text: 'срок аренды продлеваем', highlight: true, start_seconds: null },
      ],
      timecode_seconds: 760,
      image: null,
      ...patch,
    });
    server.on('GET /api/kb/documents/rec-1/text', (_body, url) =>
      ok(url.searchParams.get('page') === '3' ? part(3, { segments: [], timecode_seconds: 1200 }) : part(2, {})),
    );
    server.on('GET /api/kb/documents/rec-1', () =>
      ok({ ...KB_DOC, id: 'rec-1', title: 'Совещание 03.10.mp3', scope: 'ministries', kind: 'recording', page_count: null, duration_seconds: 4320 }),
    );

    const card = (await screen.findByText('Совещание 03.10.mp3')).closest('button') as HTMLElement;
    expect(card).toHaveTextContent('с 12:40');
    expect(card).toHaveTextContent('Министерства');
    expect(card).not.toHaveTextContent('стр.');
    await user.click(screen.getByRole('button', { name: 'Источник 2: Совещание 03.10.mp3, с 12 минут 40 секунд' }));

    // У записи страницы нет: место — момент из карточки.
    const listen = await screen.findByRole('button', { name: 'Слушать с 12:40' });
    expect(textUrls()[0]).toBe('/api/kb/documents/rec-1/text?fragment_id=frag-2&timecode=760');
    expect(await screen.findByText(/Министерства · добавил\(а\) Петров А\. С\./)).toBeInTheDocument();
    expect(screen.getByText(/1 ч 12 мин/)).toBeInTheDocument();
    expect(screen.getByText('Часть 2 из 3')).toBeInTheDocument();
    expect(screen.getByText('срок аренды продлеваем').tagName).toBe('MARK');
    // Оригинала у записи нет.
    expect(screen.queryByRole('link', { name: texts.viewer.openOriginal })).not.toBeInTheDocument();
    expect(screen.getByText(texts.viewer.noOriginal)).toBeInTheDocument();

    // Проигрыватель встаёт на момент цитаты и ждёт нажатия: сам звук не включается.
    const audio = screen.getByLabelText('Запись Совещание 03.10.mp3') as HTMLAudioElement;
    expect(audio).toHaveAttribute('src', '/api/kb/documents/rec-1/audio');
    expect(audio).not.toHaveAttribute('autoplay');
    fireEvent.loadedMetadata(audio);
    expect(audio.currentTime).toBe(760);
    expect(play).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Слушать с 12 минут 5 секунд' }));
    expect(audio.currentTime).toBe(725);
    expect(play).toHaveBeenCalledTimes(1);
    await user.click(listen);
    expect(audio.currentTime).toBe(760);
    expect(play).toHaveBeenCalledTimes(2);

    // Листание частей: по номеру с той же цитатой, без момента; проигрыватель не переставляется.
    await user.click(screen.getByRole('button', { name: texts.viewer.nextPart }));
    expect(await screen.findByText(texts.viewer.noSpeech)).toBeInTheDocument();
    expect(textUrls().at(-1)).toBe('/api/kb/documents/rec-1/text?page=3&fragment_id=frag-2');
    expect(screen.getByRole('button', { name: 'Слушать с 20:00' })).toBeInTheDocument();
    expect(screen.getByLabelText('Запись Совещание 03.10.mp3')).toBe(audio);
    expect(audio.currentTime).toBe(760);

    // Звук не загрузился: заметка с «Повторить», моменты остаются текстом.
    fireEvent.error(audio);
    expect(await screen.findByText(texts.viewer.audioFailed)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Слушать с/ })).not.toBeInTheDocument();
  });

  it('источник-запись: цитата на нескольких строках — в видимую часть прокручивается её начало', async () => {
    const record = source(2, { document_id: 'rec-1', document_title: 'Совещание 03.10.mp3', page: null, timecode_seconds: 760 });
    const { server, user } = setupChat([QUESTION, message({ content: 'Продлеваем [2].', sources_found: 2, sources: [record] })]);
    const text: DocumentText = {
      page: 1,
      page_count: 1,
      recognized: true,
      segments: [
        { text: 'По второму вопросу.', highlight: false, start_seconds: 725 },
        { text: 'срок аренды', highlight: true, start_seconds: 760 },
        { text: 'продлеваем', highlight: true, start_seconds: 770 },
        { text: 'до конца 2027 года', highlight: true, start_seconds: 780 },
      ],
      timecode_seconds: 760,
      image: null,
    };
    server.on('GET /api/kb/documents/rec-1/text', () => ok(text));
    server.on('GET /api/kb/documents/rec-1', () => ok({ ...KB_DOC, id: 'rec-1', kind: 'recording', page_count: null, duration_seconds: 4320 }));

    await user.click(await screen.findByRole('button', { name: 'Источник 2: Совещание 03.10.mp3, с 12 минут 40 секунд' }));
    expect(await screen.findByText('до конца 2027 года')).toBeInTheDocument();
    const scrolledTo = () => vi.mocked(Element.prototype.scrollIntoView).mock.contexts.filter((element) => (element as Element).tagName === 'MARK');
    await waitFor(() => expect(scrolledTo().length).toBeGreaterThan(0));
    expect(new Set(scrolledTo())).toEqual(new Set([screen.getByText('срок аренды')]));
  });

  it('источник-картинка: адрес строит интерфейс; под ответом — один раз; не загрузилась — убрана из ряда', async () => {
    const figure = { document_id: 'doc-3', document_title: 'Межевой план.pdf', page: 4, image_id: 'img-1', quote: 'Схема границ: пять точек' };
    const content = [
      'Границы — на схеме [3], она же [4], и чертёж [5].',
      // Картинки, которые написала сама модель, не загружаются — ни по внешнему адресу, ни по адресу портала.
      '![подлог](/api/kb/documents/doc-9/images/evil) ![пиксель](https://evil.example/p.png)',
    ].join('\n\n');
    const { server, user } = setupChat([
      QUESTION,
      message({
        content,
        sources_found: 5,
        sources: [source(3, figure), source(4, { ...figure, fragment_id: 'frag-4' }), source(5, { image_id: 'img-2', quote: 'Чертёж <b>сетки</b>' })],
      }),
    ]);
    server.on('GET /api/kb/documents/doc-3', () => ok({ ...KB_DOC, id: 'doc-3', title: 'Межевой план.pdf' }));
    server.on('GET /api/kb/documents/doc-3/text', (_body, url) =>
      ok(
        pageText(Number(url.searchParams.get('page')), [['Текст страницы', false]], {
          image: url.searchParams.get('page') === '4' ? { id: 'img-1', description: 'Схема **границ** <img src=x onerror="window.hacked = true">' } : null,
        }),
      ),
    );

    const row = (await screen.findByRole('heading', { name: t.sources.figures })).parentElement as HTMLElement;
    const sources = () => Array.from(screen.getByRole('main').querySelectorAll('img')).map((img) => img.getAttribute('src'));
    // Две сноски на одну картинку — одна картинка в ряду; в карточках — у каждой своя.
    expect(within(row).getAllByRole('button')).toHaveLength(2);
    expect(new Set(sources())).toEqual(new Set(['/api/kb/documents/doc-3/images/img-1', '/api/kb/documents/doc-5/images/img-2']));
    expect(screen.getByRole('main')).toHaveTextContent('подлог — /api/kb/documents/doc-9/images/evil');
    const picture = within(row).getByRole('button', { name: 'Картинка из документа Межевой план.pdf, источник 3' });
    expect(within(picture).getByAltText('Схема границ: пять точек')).toHaveAttribute('loading', 'lazy');
    expect(row).toHaveTextContent('[3] Межевой план.pdf, стр. 4');

    // Картинка не загрузилась: из ряда убирается, карточка остаётся со строкой «Картинка недоступна».
    for (const broken of screen.getAllByAltText('Чертёж <b>сетки</b>')) {
      fireEvent.error(broken);
    }
    expect(within(row).getAllByRole('button')).toHaveLength(1);
    expect(screen.getByText(t.sources.imageUnavailable).closest('button')).toHaveTextContent('Договор аренды № 14-А, часть 5.pdf');

    // Просмотр: картинка вместо текста страницы, описание — данные, а не разметка.
    await user.click(picture);
    expect(await screen.findByRole('heading', { name: texts.viewer.imageDescription })).toBeInTheDocument();
    expect(textUrls()[0]).toBe('/api/kb/documents/doc-3/text?page=4&fragment_id=frag-3');
    expect(screen.getByAltText('Картинка из документа Межевой план.pdf')).toHaveAttribute('src', '/api/kb/documents/doc-3/images/img-1');
    expect(screen.getByText('Схема **границ** <img src=x onerror="window.hacked = true">')).toBeInTheDocument();
    expect(document.querySelector('img[src="x"]')).toBeNull();
    expect(screen.queryByText('Текст страницы')).not.toBeInTheDocument();

    fireEvent.error(screen.getByAltText('Картинка из документа Межевой план.pdf'));
    expect(await screen.findByText(texts.viewer.imageFailed)).toBeInTheDocument();
    // На других страницах — обычный текст страницы.
    await user.click(screen.getByRole('button', { name: texts.viewer.next }));
    expect(await screen.findByText('Текст страницы')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: texts.viewer.imageDescription })).not.toBeInTheDocument();
  });

  it('миниатюра: отложенная загрузка, заданные размеры; не загрузившаяся заменяется значком', async () => {
    const png = { id: 'f-2', file_name: 'скан.png', media_type: 'image/png', page_count: 1, image_count: 1, duration_seconds: null, transcription: null, created_at: '' };
    setupChat([message({ id: 'u-1', role: 'user', content: 'Что на скане?', attachments: [png] })]);

    await screen.findByText('скан.png');
    const thumbnail = screen.getByRole('main').querySelector('img') as HTMLImageElement;
    expect(thumbnail).toHaveAttribute('loading', 'lazy');
    expect(thumbnail).toHaveAttribute('decoding', 'async');
    expect(thumbnail).toHaveAttribute('width', '48');
    expect(thumbnail).toHaveAttribute('height', '48');

    fireEvent.error(thumbnail);
    expect(screen.getByRole('main').querySelector('img')).toBeNull();
    expect(screen.getByText('скан.png')).toBeInTheDocument();
  });
});

describe('чат: настройки портала не загрузились', () => {
  const PNG: Attachment = { id: 'f-2', file_name: 'скан.png', media_type: 'image/png', page_count: 1, image_count: 1, duration_seconds: null, transcription: null, created_at: '' };

  it('вопрос и вставленная картинка уходят без проверки в браузере — пределы проверяет сервер', async () => {
    const { server, user } = setupChat([]);
    server.on('GET /api/config', () => fail(503, 'service_unavailable'));
    server.on(`POST /api/dialogs/${ID}/attachments`, () => ok(PNG, 201));
    server.on(`POST /api/dialogs/${ID}/messages`, () => sse([START, ['delta', { text: 'Ответ без настроек' }], ['done', { status: 'complete' }]]));
    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(1));

    // «Прикрепить» не блокируется; вставка картинки загружает файл.
    expect(screen.getByRole('button', { name: t.composer.attach })).toBeEnabled();
    fireEvent.paste(field(), { clipboardData: { files: [new File(['png'], 'image.png', { type: 'image/png' })] } });
    await waitFor(() => expect(screen.getByRole('main').querySelector('img')).not.toBeNull());

    await user.type(field(), 'Вопрос без настроек');
    await user.click(screen.getByRole('button', { name: t.composer.send }));
    expect(await screen.findByText('Ответ без настроек')).toBeInTheDocument();
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({
      content: 'Вопрос без настроек',
      attachment_ids: ['f-2'],
    });
  });

  it('отказ сервера при загрузке файла без настроек — текст сервера под панелью запроса, вложение убрано', async () => {
    const { server } = setupChat([]);
    server.on('GET /api/config', () => fail(503, 'service_unavailable'));
    server.on(`POST /api/dialogs/${ID}/attachments`, () => fail(422, 'unsupported_file_type'));
    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(1));

    fireEvent.paste(field(), { clipboardData: { files: [new File(['png'], 'image.png', { type: 'image/png' })] } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Сообщение сервера: unsupported_file_type.');
    expect(screen.queryByText('image.png')).not.toBeInTheDocument();
  });

  it('загрузка настроек повторяется сама каждые 5 секунд, пока не удастся', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { server } = setupChat([]);
      server.on('GET /api/config', () => fail(503, 'service_unavailable'));
      await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(1));
      await vi.advanceTimersByTimeAsync(5100);
      await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(2));

      server.on('GET /api/config', () => ok(CONFIG));
      await vi.advanceTimersByTimeAsync(5100);
      await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(3));
      // Настройки получены: повторы прекращаются.
      await vi.advanceTimersByTimeAsync(11000);
      expect(server.callsTo('GET /api/config')).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('чат: заголовки в ответе', () => {
  it('уровни сдвинуты на два: «#» — h3, «####» и глубже — h6; h1 на экране один', async () => {
    const content = ['# Первый', '## Второй', '### Третий', '#### Четвёртый', '###### Шестой'].join('\n\n');
    setupChat([QUESTION, message({ content })]);

    expect(await screen.findByRole('heading', { level: 3, name: 'Первый' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 4, name: 'Второй' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 5, name: 'Третий' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 6, name: 'Четвёртый' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 6, name: 'Шестой' })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1);
    expect(screen.queryByRole('heading', { level: 2, name: 'Второй' })).not.toBeInTheDocument();
  });

  it('таблица в ответе: блок с прокруткой достижим с клавиатуры', async () => {
    setupChat([QUESTION, message({ content: '| Участок | Площадь |\n|---|---|\n| 77:01 | 12 га |' })]);
    expect((await screen.findByRole('table')).parentElement).toHaveAttribute('tabindex', '0');
  });
});
