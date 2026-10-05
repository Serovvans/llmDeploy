import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Attachment, Dialog, Message, Source } from '../api/types';
import { fail, liveSse, mockApi, ok, session, sse } from '../test/mockApi';
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
      body: { content: 'Какой срок аренды?', attachment_ids: [], mode: 'fast', knowledge: 'none' },
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

    expect(await screen.findByText(t.errors.inProgress)).toBeInTheDocument();
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
    expect(await screen.findByRole('alert')).toHaveTextContent(t.errors.inProgress);
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
    [409, 'generation_in_progress', t.errors.inProgress],
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

    expect(await screen.findByText(t.longDialog)).toBeInTheDocument();
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
  const PDF: Attachment = { id: 'f-1', file_name: 'договор.pdf', media_type: 'application/pdf', page_count: 3, image_count: 0, created_at: '' };
  const PNG: Attachment = { id: 'f-2', file_name: 'скан.png', media_type: 'image/png', page_count: 1, image_count: 1, created_at: '' };

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
    const server = mockApi({
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
    expect(server.callsTo(`GET /api/dialogs/${ID}`)).toHaveLength(0);

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
      'Файл „макет.dwg“ не подходит. Можно прикрепить изображения JPG и PNG, PDF, DOCX, TXT и MD',
    );

    const big = new File(['x'], 'скан.pdf');
    Object.defineProperty(big, 'size', { value: 20971521 });
    await user.upload(fileInput(), big);
    expect(screen.getByRole('alert')).toHaveTextContent('Файл „скан.pdf“ больше 20 МБ. Уменьшите его или разделите на части');
    expect(server.callsTo(`POST /api/dialogs/${ID}/attachments`)).toHaveLength(0);
  });

  it.each([
    [415, 'unsupported_file_type', undefined, 'Файл „отчёт.pdf“ не подходит. Можно прикрепить изображения JPG и PNG, PDF, DOCX, TXT и MD'],
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
    fragment_id: `frag-${n}`,
    quote: `…сроком на 49 (сорок девять) лет, пункт ${n}…`,
    ...patch,
  });

  const KB_DOC = {
    id: 'doc-1',
    title: 'Договор аренды № 14-А.pdf',
    scope: 'shared',
    is_cogis: false,
    author: { full_name: 'Петров Алексей Сергеевич', is_me: false },
    created_at: '2026-09-12T09:00:00Z',
    page_count: 14,
    status: 'ready',
    error_code: null,
    progress: null,
    can_delete: false,
  };

  it('переключатель: значение уходит в запросе и запоминается; фаза «Ищу в базе знаний…»; вторая фраза пустого чата', async () => {
    const { server, user } = setupChat([]);
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    expect(await screen.findByText(/включите базу знаний под полем ввода/)).toBeInTheDocument();

    await user.click(screen.getByText('База знаний: не использовать'));
    await user.click(await screen.findByText('Общая и моя'));
    expect(screen.getByText('База знаний: общая и моя')).toBeInTheDocument();
    expect(window.localStorage.getItem('portal.chat.knowledge')).toBe('shared_and_personal');

    await user.type(field(), 'Какой срок аренды?{Enter}');
    live.push(START, ['search_started', {}]);
    expect((await screen.findAllByText(/Ищу в базе знаний/)).length).toBeGreaterThan(0);
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({ knowledge: 'shared_and_personal' });

    // Ноль найденного виден сразу по событию `sources`.
    live.push(['sources', { sources: [] }]);
    expect(await screen.findByText(t.sources.nothingFound)).toBeInTheDocument();
    live.push(['delta', { text: 'В базе знаний об этом ничего нет.' }], ['done', { status: 'complete' }]);
    live.close();
    await screen.findByRole('button', { name: t.composer.send });
    expect(screen.queryByText(t.sources.notCited)).not.toBeInTheDocument();
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
    [{ sources: [], sources_found: 0 }, t.sources.nothingFound],
  ] as const)('что под ответом после перезагрузки: %o', async (patch, text) => {
    setupChat([QUESTION, message({ content: 'Ответ без ссылок', ...patch, sources: patch.sources ? [] : null })]);
    await screen.findByText('Ответ без ссылок');
    for (const line of [t.sources.notCited, t.sources.nothingFound]) {
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
        ? ok({ page: 3, page_count: 14, recognized: false, segments: [{ text: 'Третья страница', highlight: false }] })
        : ok({
            page: 2,
            page_count: 14,
            recognized: true,
            segments: [
              { text: '1.1. Арендодатель предоставляет участок ', highlight: false },
              { text: 'сроком на 49 (сорок девять) лет', highlight: true },
              { text: ' с даты регистрации…', highlight: false },
            ],
          }),
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
    const first = vi.mocked(fetch).mock.calls.map(([input]) => String(input)).find((url) => url.includes('/text'));
    expect(first).toBe('/api/kb/documents/doc-1/text?fragment_id=frag-1');
    // Пока открыт просмотр, история скрыта, лента остаётся.
    expect(screen.queryByRole('complementary', { name: t.history })).not.toBeInTheDocument();
    expect(screen.getByText('Какой срок аренды?')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: texts.viewer.next }));
    expect(await screen.findByText('Третья страница')).toBeInTheDocument();
    expect(screen.queryByText(texts.viewer.recognized)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('href', '/api/kb/documents/doc-1/file#page=3');

    // При листании цитата передаётся вместе со страницей: вернувшись, видим её подсвеченной.
    const textUrls = () => vi.mocked(fetch).mock.calls.map(([input]) => String(input)).filter((url) => url.includes('/text'));
    expect(textUrls().at(-1)).toBe('/api/kb/documents/doc-1/text?page=3&fragment_id=frag-1');
    server.on('GET /api/kb/documents/doc-1/text', (_body, url) =>
      ok({
        page: 2,
        page_count: 14,
        recognized: true,
        segments: [{ text: 'сроком на 49 (сорок девять) лет', highlight: url.searchParams.get('fragment_id') === 'frag-1' }],
      }),
    );
    await user.click(screen.getByRole('button', { name: texts.viewer.previous }));
    await waitFor(() => expect(textUrls().at(-1)).toBe('/api/kb/documents/doc-1/text?page=2&fragment_id=frag-1'));
    expect((await screen.findByText('сроком на 49 (сорок девять) лет')).tagName).toBe('MARK');
  });

  it('документ без страниц: навигации нет; сведения не получены — шапка с одним названием', async () => {
    const { server, user } = setupChat([QUESTION, message({ content: 'Срок [1].', sources_found: 1, sources: [source(1, { page: null })] })]);
    server.on('GET /api/kb/documents/doc-1/text', () =>
      ok({ page: null, page_count: null, recognized: false, segments: [{ text: 'Весь текст документа', highlight: false }] }),
    );
    server.on('GET /api/kb/documents/doc-1', () => fail(500, 'internal_error'));

    await user.click(await screen.findByRole('button', { name: /Источник 1/ }));
    expect(await screen.findByText('Весь текст документа')).toBeInTheDocument();
    expect(screen.queryByText(/Страница/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Общая база/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('href', '/api/kb/documents/doc-1/file');
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
    const second = ok({ page: 2, page_count: 14, recognized: false, segments: [{ text: 'Вторая страница', highlight: false }] });
    server.on('GET /api/kb/documents/doc-1', () => ok(KB_DOC));
    server.on('GET /api/kb/documents/doc-1/text', (_body, url) => (url.searchParams.has('page') ? fail(500, 'internal_error') : second));

    await user.click(await screen.findByRole('button', { name: /Источник 1/ }));
    expect(await screen.findByText('Вторая страница')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: texts.viewer.next }));
    expect(await screen.findByText('Не удалось открыть страницу')).toBeInTheDocument();
    // Прежняя страница остаётся на месте.
    expect(screen.getByText('Вторая страница')).toBeInTheDocument();

    server.on('GET /api/kb/documents/doc-1/text', (_body, url) =>
      url.searchParams.get('page') === '3'
        ? ok({ page: 3, page_count: 14, recognized: false, segments: [{ text: 'Третья страница', highlight: false }] })
        : second,
    );
    await user.click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByText('Третья страница')).toBeInTheDocument();
    expect(screen.queryByText('Не удалось открыть страницу')).not.toBeInTheDocument();

    server.on('GET /api/kb/documents/doc-1/text', (_body, url) => (url.searchParams.has('page') ? fail(404, 'not_found') : second));
    await user.click(screen.getByRole('button', { name: texts.viewer.previous }));
    expect(await screen.findByText(texts.viewer.deleted)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Третья страница')).not.toBeInTheDocument());
    expect(screen.getByRole('complementary', { name: t.history })).toBeInTheDocument();
  });

  it('миниатюра: отложенная загрузка, заданные размеры; не загрузившаяся заменяется значком', async () => {
    const png = { id: 'f-2', file_name: 'скан.png', media_type: 'image/png', page_count: 1, image_count: 1, created_at: '' };
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
