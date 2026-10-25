import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { Dialog, DialogKind, DocparseResult, Memory, MemoryPair, Message, Source } from '../api/types';
import { CONFIG, fail, liveSse, mockApi, ok, session } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { texts } from '../texts';

const t = texts.memory;
const ID = 'd-1';
const MEMORY_CONFIG = { ...CONFIG, memory: { ...CONFIG.memory, enabled: true } };
const START = ['start', { user_message_id: 'u-3', assistant_message_id: 'a-3' }] as [string, object];

function dialog(kind: DialogKind = 'chat'): Dialog {
  const now = new Date().toISOString();
  return { id: ID, kind, title: 'Аренда участка под ЛЭП', created_at: now, updated_at: now };
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
    memory_mode: 'full',
    created_at: '2026-10-05T09:01:00Z',
    ...patch,
  };
}

const ATTACHMENT = {
  id: 'f-1',
  file_name: 'договор.pdf',
  media_type: 'application/pdf',
  page_count: 3,
  image_count: 0,
  duration_seconds: null,
  transcription: null,
  created_at: '2026-10-05T09:02:00Z',
};

/** Две пары: вторая — с вложением. */
const MESSAGES = [
  message({ id: 'u-1', role: 'user', content: 'Какой срок аренды?', created_at: '2026-10-05T09:00:00Z' }),
  message({ id: 'a-1', content: 'Срок аренды — 49 лет.', created_at: '2026-10-05T09:01:00Z' }),
  message({ id: 'u-2', role: 'user', content: '', attachments: [ATTACHMENT], created_at: '2026-10-05T09:02:00Z' }),
  message({ id: 'a-2', content: 'В договоре три страницы.', created_at: '2026-10-05T09:03:00Z' }),
];

function pair(n: number, patch: Partial<MemoryPair> = {}): MemoryPair {
  return {
    question_id: `u-${n}`,
    answer_id: `a-${n}`,
    question_preview: 'Какой срок аренды?',
    answer_preview: 'Срок аренды — 49 лет.',
    attachments: 0,
    memory_mode: 'full',
    tokens: 2400,
    dropped: false,
    summarized: false,
    ...patch,
  };
}

function estimate(patch: Partial<Memory> = {}): Memory {
  return {
    level: 'filling',
    percent: 63,
    used_tokens: 18000,
    limit_tokens: 28500,
    compress_pairs: null,
    pairs: [pair(1), pair(2, { question_preview: '', attachments: 1 })],
    ...patch,
  };
}

function setup(memory: () => ReturnType<typeof ok>, options: { kind?: DialogKind; messages?: Message[] } = {}) {
  const kind = options.kind ?? 'chat';
  const messages = options.messages ?? MESSAGES;
  const server = mockApi({
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/config': () => ok(MEMORY_CONFIG),
    'GET /api/dialogs': () => ok({ items: [dialog(kind)], next_cursor: null }),
    [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [...messages].reverse(), next_cursor: null }),
    [`GET /api/dialogs/${ID}/memory`]: memory,
  });
  const base = { chat: '/chat', sql: '/sql', cogis: '/cogis', docparse: '/documents', code: '/code' }[kind];
  renderApp(`${base}/${ID}`);
  return { server, user: userEvent.setup({ applyAccept: false }) };
}

/** Строки запроса оценок памяти по порядку. */
function memoryQueries(): URLSearchParams[] {
  return vi
    .mocked(fetch)
    .mock.calls.map(([input]) => new URL(String(input), 'http://localhost'))
    .filter((url) => url.pathname.endsWith('/memory'))
    .map((url) => url.searchParams);
}

function panel(): HTMLElement {
  return screen.getByRole('complementary', { name: t.title });
}

function toggle(): HTMLElement {
  return screen.getByRole('button', { name: t.title });
}

async function choose(n: number, label: string, user: ReturnType<typeof userEvent.setup>) {
  await user.click(await within(panel()).findByLabelText(t.modeLabel(n)));
  const item = await waitFor(() => {
    const found = menuItems().find((candidate) => candidate.textContent?.startsWith(label));
    expect(found).toBeDefined();
    return found as HTMLElement;
  });
  await user.click(item);
}

describe('память диалога: индикатор и панель', () => {
  it.each(['light', 'dark'] as const)(
    'при первом входе панель открыта; уровень — из ответа сервера, процент — в подсказке (тема %s)',
    async (theme) => {
      window.localStorage.setItem('portal.theme', theme);
      // Уровень «заполняется» при 92 %: интерфейс сам по проценту уровень не вычисляет (CLAUDE.md, правило 1).
      setup(() => ok(estimate({ percent: 92, pairs: [pair(1, { dropped: true }), pair(2, { question_preview: '', attachments: 2 })] })));

      const meter = await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.filling}` }, { timeout: 5000 });
      expect(document.documentElement.dataset.theme).toBe(theme);
      expect(meter).toHaveAttribute('aria-expanded', 'true');
      expect(document.getElementById(meter.getAttribute('aria-describedby') ?? '')?.textContent).toBe(t.percentHint(92));
      expect(meter).not.toHaveTextContent('92');

      const memory = await screen.findByRole('complementary', { name: t.title });
      expect(within(memory).getByText(t.levelTitles.filling)).toBeInTheDocument();
      expect(within(memory).getByText(t.explain.dialog.filling)).toBeInTheDocument();
      expect(within(memory).getByText(t.chatNote)).toBeInTheDocument();
      expect(within(memory).getByRole('button', { name: t.showInFeed(1, 'Какой срок аренды?') })).toBeInTheDocument();
      expect(within(memory).getByRole('button', { name: t.showInFeed(2, t.onlyAttachments) })).toBeInTheDocument();
      expect(within(memory).getByText(t.attachments(2))).toBeInTheDocument();
      // «Не поместится» — у учитываемого вопроса, который выпадет со следующим вопросом.
      expect(within(memory).getByText(t.wontFit)).toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-expanded', 'true');

      // Скрыть кнопкой в строке заголовка: место освобождается, индикатор остаётся, выбор запоминается.
      const user = userEvent.setup();
      await user.click(toggle());
      expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
      expect(toggle()).toHaveAttribute('aria-expanded', 'false');
      expect(toggle()).toHaveFocus();
      expect(screen.getByRole('button', { name: `${t.meterCaption} ${t.levels.filling}` })).toBeInTheDocument();
      expect(window.localStorage.getItem('portal.memoryPanel')).toBe('hidden');
    },
  );

  it('скрытая панель остаётся скрытой после перезагрузки; индикатор открывает её и ставит фокус на заголовок, Esc закрывает', async () => {
    window.localStorage.setItem('portal.memoryPanel', 'hidden');
    const { user } = setup(() => ok(estimate()));

    const meter = await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.filling}` });
    expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
    await user.click(meter);
    expect(await within(panel()).findByRole('heading', { level: 2, name: t.title })).toHaveFocus();
    expect(window.localStorage.getItem('portal.memoryPanel')).toBe('open');

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
    // Фокус — туда, откуда панель открыли.
    expect(meter).toHaveFocus();
  });

  it('смена режима и баз знаний пересчитывает оценку через 300 мс; уровень и заметка следуют ответу', async () => {
    let level: Memory['level'] = 'filling';
    const { user } = setup(() => ok(estimate({ level })));
    await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.filling}` });
    expect(memoryQueries().map((query) => query.toString())).toEqual(['mode=fast&search=false']);
    expect(screen.queryByText(t.longDialog.dialog.almost_full)).not.toBeInTheDocument();

    level = 'almost_full';
    await user.click(screen.getByText(texts.chat.composer.modeThorough));
    expect(await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.almost_full}` })).toBeInTheDocument();
    expect(memoryQueries().at(-1)?.toString()).toBe('mode=thorough&search=false');

    // С «почти заполнена» — заметка о длинном диалоге с двумя действиями.
    const notice = screen.getByText(t.longDialog.dialog.almost_full).closest('[role="status"]') as HTMLElement;
    expect(within(notice).getByRole('button', { name: t.openPanel })).toBeInTheDocument();
    expect(within(notice).getByRole('button', { name: texts.dialogs.chat.newLabel })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /^База знаний/ }));
    await user.click(await screen.findByRole('checkbox', { name: /^Общая —/ }));
    await waitFor(() => expect(memoryQueries().at(-1)?.toString()).toBe('mode=thorough&search=true'));
  });

  it('выбор у вопроса: сразу в описи и в ленте, уходит на сервер, оценка пересчитывается; отказ 409 возвращает прежний', async () => {
    const { server, user } = setup(() => ok(estimate()));
    server.on(`PATCH /api/dialogs/${ID}/messages/u-1`, () => ok(message({ id: 'u-1', role: 'user', memory_mode: 'excluded' })));
    await screen.findByRole('complementary', { name: t.title });
    const before = memoryQueries().length;

    await choose(1, t.modes.excluded, user);
    expect(server.callsTo(`PATCH /api/dialogs/${ID}/messages/u-1`)[0]?.body).toEqual({ memory_mode: 'excluded' });
    // Пара в ленте приглушена, пометка ведёт в панель.
    expect(await screen.findByRole('button', { name: t.excludedLabel })).toBeInTheDocument();
    await waitFor(() => expect(memoryQueries().length).toBe(before + 1));

    // У вопроса с вложениями — три пункта; без вложений «Без вложений» нет.
    await user.click(within(panel()).getByLabelText(t.modeLabel(2)));
    await waitFor(() => expect(menuItems().map((item) => item.textContent)).toEqual([
      `${t.modes.full}${t.modeComments.full}`,
      `${t.modes.text_only}${t.modeComments.text_only}`,
      `${t.modes.excluded}${t.modeComments.excluded}`,
    ]));
    await user.keyboard('{Escape}');
    // Esc закрыл список, а не панель.
    expect(panel()).toBeInTheDocument();

    server.on(`PATCH /api/dialogs/${ID}/messages/u-2`, () => fail(409, 'generation_in_progress'));
    await choose(2, t.modes.text_only, user);
    expect(await within(panel()).findByRole('alert')).toHaveTextContent(t.changeBusy);
    expect(within(panel()).getByLabelText(t.modeLabel(2))).toHaveTextContent(t.modes.full);
    expect(screen.queryByText(t.textOnly)).not.toBeInTheDocument();
  });

  it('во время ответа выбор недоступен и сказано почему; `context_truncated` сразу даёт «переполнена»; после ответа — новая оценка', async () => {
    const live = liveSse();
    const { server, user } = setup(() => ok(estimate()));
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('complementary', { name: t.title });
    const before = memoryQueries().length;

    await user.type(screen.getByPlaceholderText(texts.chat.composer.placeholder), 'А арендная плата?{Enter}');
    expect(await within(panel()).findByText(t.busy)).toBeInTheDocument();
    expect(within(panel()).getByLabelText(t.modeLabel(1))).toBeDisabled();

    live.push(START, ['context_truncated', { dropped_messages: 2 }]);
    expect(await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.overflow}` })).toBeInTheDocument();
    expect(screen.getByText(t.longDialog.dialog.dropped)).toBeInTheDocument();
    expect(memoryQueries().length).toBe(before);

    server.on(`GET /api/dialogs/${ID}/memory`, () => ok(estimate({ level: 'almost_full' })));
    live.push(['delta', { text: '120 000 руб.' }], ['done', { status: 'complete' }]);
    live.close();
    expect(await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.almost_full}` })).toBeInTheDocument();
    expect(memoryQueries().length).toBe(before + 1);
    expect(within(panel()).queryByText(t.busy)).not.toBeInTheDocument();
  });

  it('пометка «Модель не учитывает» в ленте открывает панель на выборе этого вопроса', async () => {
    window.localStorage.setItem('portal.memoryPanel', 'hidden');
    const excluded = MESSAGES.map((item) => (item.id === 'u-1' || item.id === 'a-1' ? { ...item, memory_mode: 'excluded' as const } : item));
    const { user } = setup(() => ok(estimate({ pairs: [pair(1, { memory_mode: 'excluded' }), pair(2, { attachments: 1 })] })), {
      messages: excluded,
    });

    await user.click(await screen.findByRole('button', { name: t.excludedLabel }));
    await waitFor(() => expect(within(panel()).getByLabelText(t.modeLabel(1))).toHaveFocus());
  });

  it('просмотр источника занимает место панели; кнопка «Память диалога» закрывает просмотр и возвращает панель', async () => {
    const source: Source = {
      n: 1,
      document_id: 'doc-1',
      document_title: 'Договор аренды № 14-А.pdf',
      scope: 'shared',
      page: 2,
      timecode_seconds: null,
      image_id: null,
      fragment_id: 'frag-1',
      quote: '…сроком на 49 (сорок девять) лет…',
    };
    const { server, user } = setup(() => ok(estimate({ pairs: [pair(1)] })), {
      messages: [MESSAGES[0] as Message, message({ id: 'a-1', content: 'Срок [1].', sources_found: 1, sources: [source] })],
    });
    server.on('GET /api/kb/documents/doc-1/text', () =>
      ok({ page: 2, page_count: 14, recognized: false, segments: [{ text: 'Текст', highlight: true, start_seconds: null }], timecode_seconds: null, image: null }),
    );
    server.on('GET /api/kb/documents/doc-1', () => fail(404, 'not_found'));
    await screen.findByRole('complementary', { name: t.title });

    await user.click(await screen.findByRole('button', { name: /Источник 1/ }));
    await screen.findByRole('link', { name: texts.viewer.openOriginal });
    expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
    // Запомненное положение не меняется.
    expect(window.localStorage.getItem('portal.memoryPanel')).toBeNull();

    await user.click(toggle());
    expect(await screen.findByRole('complementary', { name: t.title })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: texts.viewer.openOriginal })).not.toBeInTheDocument();
    await waitFor(() => expect(within(panel()).getByRole('heading', { level: 2, name: t.title })).toHaveFocus());
  });

  it('функция выключена: ни индикатора, ни кнопки, ни панели, оценка не запрашивается; приглушение остаётся текстом', async () => {
    const excluded = MESSAGES.map((item) => (item.id === 'u-1' || item.id === 'a-1' ? { ...item, memory_mode: 'excluded' as const } : item));
    const { server } = setup(() => ok(estimate()), { messages: excluded });
    server.on('GET /api/config', () => ok(CONFIG));

    expect(await screen.findByText(t.excluded)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.excludedLabel })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.title })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: new RegExp(`^${t.meterCaption}`) })).not.toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
    expect(memoryQueries()).toHaveLength(0);
  });

  it('функцию выключили, пока экран открыт (403): всё памяти уходит, настройки перечитываются, тост — один раз', async () => {
    const { server } = setup(() => fail(403, 'feature_disabled'));

    expect(await screen.findByText(t.disabled)).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.title })).not.toBeInTheDocument();
    await waitFor(() => expect(server.callsTo('GET /api/config').length).toBeGreaterThan(1));
  });

  it('сбой оценки: «оценка недоступна» у поля ввода, в панели — заметка с «Повторить»', async () => {
    const { server, user } = setup(() => fail(500, 'internal_error'));

    expect(await screen.findByRole('button', { name: `${t.meterCaption} ${t.unavailable}` })).toBeInTheDocument();
    server.on(`GET /api/dialogs/${ID}/memory`, () => ok(estimate()));
    await user.click(within(panel()).getByRole('button', { name: texts.common.retry }));
    expect(await within(panel()).findByText(t.levelTitles.filling)).toBeInTheDocument();
  });
});

describe('память диалога в инструментах', () => {
  it('SQL: оценка с действием, диалектом и схемой; проверка на тестовых данных в строку не входит', async () => {
    window.localStorage.setItem('portal.sql.schema', 's-1');
    const { server } = setup(() => ok(estimate({ pairs: [pair(1)] })), { kind: 'sql', messages: MESSAGES.slice(0, 2) });
    server.on('GET /api/sql/schemas', () => ok({ items: [{ id: 's-1', name: 'Земельный кадастр', updated_at: '2026-10-01T00:00:00Z' }] }));

    await screen.findByRole('complementary', { name: t.title });
    await waitFor(() => expect(memoryQueries().at(-1)?.toString()).toBe('action=write&dialect=postgres&schema_id=s-1'));
    expect(within(panel()).queryByText(t.chatNote)).not.toBeInTheDocument();
  });

  it('SQL: схема удалена в другой вкладке — список обновляется, оценка повторяется без схемы', async () => {
    window.localStorage.setItem('portal.sql.schema', 's-1');
    let schemas = [{ id: 's-1', name: 'Земельный кадастр', updated_at: '2026-10-01T00:00:00Z' }];
    const { server } = setup(
      () => ok(estimate({ pairs: [pair(1)] })),
      { kind: 'sql', messages: MESSAGES.slice(0, 2) },
    );
    server.on('GET /api/sql/schemas', () => ok({ items: schemas }));
    server.on(`GET /api/dialogs/${ID}/memory`, (_body, url) => {
      if (url.searchParams.has('schema_id')) {
        schemas = [];
        return fail(422, 'schema_not_found');
      }
      return ok(estimate({ pairs: [pair(1)] }));
    });

    expect(await screen.findByText(texts.sql.schemas.alreadyRemoved)).toBeInTheDocument();
    await waitFor(() => expect(memoryQueries().at(-1)?.toString()).toBe('action=write&dialect=postgres'));
    expect(await screen.findByRole('button', { name: `${t.meterCaption} ${t.levels.filling}` })).toBeInTheDocument();
  });

  it('CoGIS: оценка с действием', async () => {
    setup(() => ok(estimate({ pairs: [pair(1)] })), { kind: 'cogis', messages: MESSAGES.slice(0, 2) });
    await screen.findByRole('complementary', { name: t.title });
    // The screen mounts only after the portal settings arrive, together with the panel: the estimate follows it.
    await waitFor(() => expect(memoryQueries().at(-1)?.toString()).toBe('action=write'));
  });

  it('разбор документа: панель на экране результата, без параметров; на форме её нет', async () => {
    const result: DocparseResult = {
      file_name: 'выписка.pdf',
      page_count: 3,
      template_id: 'egrn',
      template_title: 'Выписка ЕГРН',
      free_form: false,
      fields: [{ title: 'Кадастровый номер', value: '77:01:0004012:345' }],
      summary: 'Выписка подтверждает право собственности.',
      summary_status: 'complete',
    };
    const { server, user } = setup(() => ok(estimate({ level: 'overflow', percent: 104, pairs: [pair(1)] })), {
      kind: 'docparse',
      messages: MESSAGES.slice(0, 2),
    });
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result }));

    const memory = await screen.findByRole('complementary', { name: t.title });
    expect(await within(memory).findByText(t.docparseNote)).toBeInTheDocument();
    expect(within(memory).getByText(t.explain.docparse.overflow)).toBeInTheDocument();
    expect(screen.getByText(t.longDialog.docparse.overflow)).toBeInTheDocument();
    expect(memoryQueries().at(-1)?.toString()).toBe('');
    // У разбора действие одно: нового разбора того же документа заметка не предлагает.
    const notice = screen.getByText(t.longDialog.docparse.overflow).closest('[role="status"]') as HTMLElement;
    expect(within(notice).getAllByRole('button').map((button) => button.textContent)).toEqual([t.openPanel]);

    await user.click(screen.getByRole('button', { name: texts.dialogs.docparse.newLabel }));
    await screen.findByText(texts.docparse.pick);
    expect(screen.queryByRole('complementary', { name: t.title })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.title })).not.toBeInTheDocument();
  });
});

describe('память диалога 0.8: сжатие, перенос, «Продолжить отсюда»', () => {
  /** Три пары: сжать можно первую, две последние не сжимаются (`keep_recent_pairs` = 2). */
  const THREE = [
    ...MESSAGES,
    message({ id: 'u-3', role: 'user', content: 'А арендная плата?', created_at: '2026-10-05T09:04:00Z' }),
    message({ id: 'a-3', content: '120 000 руб. в год.', created_at: '2026-10-05T09:05:00Z' }),
  ];
  const COMPRESSIBLE = () => ok(estimate({ compress_pairs: { from: 1, to: 1 }, pairs: [pair(1), pair(2), pair(3)] }));
  const SUMMARY = { text: 'Обсуждали срок аренды: 49 лет.', until_message_id: 'a-1', messages: 2 };
  const draftRoute = `POST /api/dialogs/${ID}/memory/draft`;

  it.each(['light', 'dark'] as const)(
    'сжатие: черновик потоком → «Применить» с границей из start → тост, граница в ленте, новая оценка (тема %s)',
    async (theme) => {
      window.localStorage.setItem('portal.theme', theme);
      const live = liveSse();
      const { server, user } = setup(COMPRESSIBLE, { messages: THREE });
      server.on(draftRoute, () => live.response);
      server.on(`PUT /api/dialogs/${ID}/memory/summary`, (body) => ok({ ...(body as object), messages: 2 }));

      await user.click(await within(await screen.findByRole('complementary', { name: t.title })).findByRole('button', { name: t.summary.compress }));
      const modal = await screen.findByRole('dialog');
      expect(document.documentElement.dataset.theme).toBe(theme);
      expect(server.callsTo(draftRoute)[0]?.body).toEqual({ purpose: 'compress' });
      live.push(['start', { until_message_id: 'a-1', messages: 2, dropped_messages: 0 }], ['delta', { text: 'Обсуждали ' }]);
      expect(await within(modal).findByText(t.draft.streaming)).toBeInTheDocument();
      // Во время потока применить нельзя: сказано почему.
      await user.click(within(modal).getByRole('button', { name: t.compress.apply }));
      expect(await within(modal).findByText(t.draft.wait)).toBeInTheDocument();
      expect(server.callsTo(`PUT /api/dialogs/${ID}/memory/summary`)).toHaveLength(0);

      live.push(['delta', { text: 'срок аренды: 49 лет.' }], ['done', { truncated: false }]);
      live.close();
      await waitFor(() => expect(within(modal).getByRole('textbox', { name: t.summary.title })).toHaveValue(SUMMARY.text));
      expect(within(modal).getByRole('textbox', { name: t.summary.title })).not.toHaveAttribute('readonly');
      const before = memoryQueries().length;
      await user.click(within(modal).getByRole('button', { name: t.compress.apply }));

      expect(await screen.findByText(t.summary.applied)).toBeInTheDocument();
      expect(server.callsTo(`PUT /api/dialogs/${ID}/memory/summary`)[0]?.body).toEqual({ text: SUMMARY.text, until_message_id: 'a-1' });
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(screen.getByText(t.summary.boundary(2), { exact: false })).toBeInTheDocument();
      await waitFor(() => expect(memoryQueries().length).toBe(before + 1));
    },
  );

  it('«Остановить» оставляет полученный текст для правки; 429 — заметка с «Повторить»', async () => {
    const live = liveSse();
    const { server, user } = setup(COMPRESSIBLE, { messages: THREE });
    server.on(draftRoute, () => fail(429, 'draft_in_progress'));

    await user.click(await within(await screen.findByRole('complementary', { name: t.title })).findByRole('button', { name: t.summary.compress }));
    const modal = await screen.findByRole('dialog');
    expect(await within(modal).findByText(t.draft.draftInProgress)).toBeInTheDocument();

    server.on(draftRoute, () => live.response);
    await user.click(within(modal).getByRole('button', { name: texts.common.retry }));
    live.push(['start', { until_message_id: 'a-1', messages: 2, dropped_messages: 0 }], ['delta', { text: 'Обсуждали срок' }]);
    await user.click(await within(modal).findByRole('button', { name: t.draft.stop }));
    expect(await within(modal).findByText(t.draft.stopped)).toBeInTheDocument();
    const field = within(modal).getByRole('textbox', { name: t.summary.title });
    expect(field).toHaveValue('Обсуждали срок');
    expect(field).not.toHaveAttribute('readonly');
    expect(within(modal).queryByText(t.draft.draftInProgress)).not.toBeInTheDocument();
  });

  it('отмена сжатия: подтверждение → DELETE → граница уходит из ленты, тост', async () => {
    const { server, user } = setup(
      () => ok(estimate({ pairs: [pair(1, { summarized: true }), pair(2), pair(3)] })),
      { messages: THREE },
    );
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog(), summary: SUMMARY, carried: null }));
    server.on(`DELETE /api/dialogs/${ID}/memory/summary`, () => ok());

    expect(await screen.findByText(t.summary.boundary(2), { exact: false })).toBeInTheDocument();
    const memory = await screen.findByRole('complementary', { name: t.title });
    expect(within(memory).getByRole('button', { name: t.summary.collapsed(1, 1) })).toBeInTheDocument();
    await user.click(within(memory).getByRole('button', { name: t.summary.cancel }));
    const confirm = await screen.findByRole('dialog');
    await user.click(within(confirm).getByRole('button', { name: t.summary.cancel }));

    expect(await screen.findByText(t.summary.cancelled)).toBeInTheDocument();
    expect(server.callsTo(`DELETE /api/dialogs/${ID}/memory/summary`)).toHaveLength(1);
    expect(screen.queryByText(t.summary.boundary(2), { exact: false })).not.toBeInTheDocument();
  });

  it('перенос из меню диалога, «Один ответ дословно»: новый диалог с carried, переход и заметка о перенесённом', async () => {
    const { server, user } = setup(COMPRESSIBLE, { messages: THREE });
    const now = new Date().toISOString();
    const created: Dialog = { id: 'd-2', kind: 'chat', title: null, created_at: now, updated_at: now };
    server.on('POST /api/dialogs', () => ok(created, 201));
    server.on('GET /api/dialogs/d-2', () =>
      ok({ ...created, summary: null, carried: { text: '120 000 руб. в год.', source_dialog_id: ID, source_kind: 'chat', source_title: 'Аренда участка под ЛЭП' } }),
    );
    server.on('GET /api/dialogs/d-2/messages', () => ok({ items: [], next_cursor: null }));
    server.on('GET /api/dialogs/d-2/memory', () => ok(estimate({ level: 'free', percent: 2, pairs: [] })));
    await screen.findByRole('complementary', { name: t.title });

    await user.click(within(screen.getByRole('main')).getAllByRole('button', { name: 'Действия: Аренда участка под ЛЭП' }).at(-1) as HTMLElement);
    await user.click(await waitFor(() => menuItems().find((item) => item.textContent === t.transfer.menu) as HTMLElement));
    const modal = await screen.findByRole('dialog');
    expect(within(modal).getByText(t.transfer.notTransferred)).toBeInTheDocument();
    await user.click(within(modal).getByText(t.transfer.verbatim));
    // Дословно — последний ответ, без участия модели.
    await waitFor(() => expect(within(modal).getByRole('textbox', { name: t.transfer.verbatim })).toHaveValue('120 000 руб. в год.'));
    await user.click(within(modal).getByRole('button', { name: t.transfer.submit }));

    expect(server.callsTo('POST /api/dialogs')[0]?.body).toEqual({
      kind: 'chat',
      carried: { text: '120 000 руб. в год.', source_dialog_id: ID },
    });
    expect(server.callsTo(draftRoute)).toHaveLength(0);
    const notice = await screen.findByText(t.carried.notice, { exact: false });
    expect(within(notice.closest('[role="status"]') as HTMLElement).getByRole('link', { name: 'Аренда участка под ЛЭП' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('«Продолжить отсюда» у ответа: окно без «Что взять», черновик до этого ответа', async () => {
    const live = liveSse();
    const { server, user } = setup(COMPRESSIBLE, { messages: THREE });
    server.on(draftRoute, () => live.response);
    await screen.findByRole('complementary', { name: t.title });

    const actions = await screen.findAllByRole('button', { name: t.transfer.continue });
    expect(actions).toHaveLength(3);
    await user.click(actions[1] as HTMLElement);
    const modal = await screen.findByRole('dialog');
    expect(within(modal).getByText(t.transfer.continueTitle)).toBeInTheDocument();
    expect(within(modal).queryByText(t.transfer.what)).not.toBeInTheDocument();
    await user.click(within(modal).getByRole('button', { name: t.transfer.prepare }));
    expect(server.callsTo(draftRoute)[0]?.body).toEqual({ purpose: 'transfer', until_message_id: 'a-2' });
  });

  it('функция выключена: «Продолжить отсюда» и переноса нет; граница пересказа и заметка о перенесённом остаются', async () => {
    const { server, user } = setup(() => ok(estimate()), { messages: THREE });
    server.on('GET /api/config', () => ok(CONFIG));
    server.on(`GET /api/dialogs/${ID}`, () =>
      ok({ ...dialog(), summary: SUMMARY, carried: { text: 'Сведения', source_dialog_id: null, source_kind: null, source_title: null } }),
    );

    expect(await screen.findByText(t.summary.boundary(2), { exact: false })).toBeInTheDocument();
    expect(screen.getByText(t.carried.notice, { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.transfer.continue })).not.toBeInTheDocument();
    await user.click(within(screen.getByRole('main')).getAllByRole('button', { name: 'Действия: Аренда участка под ЛЭП' }).at(-1) as HTMLElement);
    await waitFor(() => expect(menuItems().map((item) => item.textContent)).toContain(texts.chat.menu.rename));
    expect(menuItems().map((item) => item.textContent)).not.toContain(t.transfer.menu);
  });
});
