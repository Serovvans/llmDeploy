import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { DbConnection, DbSnapshot, Dialog, Message, PortalConfig, SqlRunBlock } from '../api/types';
import { CONFIG, liveSse, mockApi, ok, session, sse } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

const d = texts.db;
const t = texts.sql;
const ID = 'd-1';
const DB_CONFIG: PortalConfig = { ...CONFIG, db: { ...CONFIG.db, enabled: true } };
const START = ['start', { user_message_id: 'u-1', assistant_message_id: 'a-1' }] as [string, object];

const CONNECTION: DbConnection = {
  id: 'c-1',
  title: 'Кадастр (рабочая)',
  description: 'Участки, договоры аренды',
  usable: true,
  samples_allowed: true,
  snapshot: { taken_at: '2026-10-25T11:05:00Z', tables: 2, truncated: false },
  refresh_available_at: null,
};

const SNAPSHOT: DbSnapshot = {
  connection_id: 'c-1',
  taken_at: '2026-10-25T11:05:00Z',
  truncated: false,
  tables: [
    { schema: 'cadastre', name: 'parcels', kind: 'table', comment: 'Земельные участки', samples_excluded: false, columns: [] },
    { schema: 'cadastre', name: 'owners', kind: 'table', comment: '<b>Правообладатели</b>', samples_excluded: true, columns: [] },
  ],
};

function dialog(patch: Partial<Dialog> = {}): Dialog {
  const now = new Date().toISOString();
  return { id: ID, kind: 'sql', title: null, created_at: now, updated_at: now, ...patch };
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
    created_at: '2026-10-25T09:01:00Z',
    ...patch,
  };
}

function setup(path: string, handlers: Parameters<typeof mockApi>[0] = {}) {
  const server = mockApi({
    'GET /api/config': () => ok(DB_CONFIG),
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/dialogs': () => ok({ items: [], next_cursor: null }),
    'GET /api/sql/schemas': () => ok({ items: [] }),
    'GET /api/db/connections': () => ok({ items: [CONNECTION] }),
    'GET /api/db/connections/c-1/snapshot': () => ok(SNAPSHOT),
    [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
    ...handlers,
  });
  renderApp(path);
  return { server, user: userEvent.setup() };
}

describe('SQL-помощник с подключением к базе', () => {
  it.each(['light', 'dark'] as const)('выбор подключения создаёт диалог и сохраняет выбор; вопрос — без схемы, диалект песочницы (тема %s)', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    window.localStorage.setItem('portal.sql.dialect', 'mssql');
    let selection: Dialog['db'] = null;
    const { server, user } = setup('/sql', {
      'POST /api/dialogs': () => ok(dialog(), 201),
      [`PATCH /api/dialogs/${ID}`]: (body) => {
        const db = (body as { db: { connection_id: string; tables: [] } }).db;
        selection = { ...db, title: CONNECTION.title, available: true };
        return ok(dialog({ db: selection }));
      },
      [`GET /api/dialogs/${ID}`]: () => ok(dialog({ db: selection })),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['delta', { text: 'Готово' }], ['done', { status: 'complete' }]]),
    });

    expect(await screen.findByText(d.sql.emptyText)).toBeInTheDocument();
    expect(screen.getByText(d.sql.noteWithRun)).toBeInTheDocument();
    await user.click(screen.getByText(t.noSchema));
    expect(await screen.findByText(d.groupDatabases)).toBeInTheDocument();
    expect(screen.getByText(CONNECTION.description)).toBeInTheDocument();
    await user.click(screen.getByText(CONNECTION.title));

    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent(`/sql/${ID}`));
    expect(server.callsTo(`PATCH /api/dialogs/${ID}`)[0]?.body).toEqual({ db: { connection_id: 'c-1', tables: [] } });
    expect(window.localStorage.getItem('portal.sql.schema')).toBe('db:c-1');
    // До первого вопроса экран — как новый вопрос.
    expect(screen.getByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: d.chooseTables })).toBeInTheDocument();
    // Диалект — только песочницы: список недоступен, но в обходе и с пояснением.
    expect(screen.getByRole('button', { name: `${t.dialect}: PostgreSQL + PostGIS` })).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByRole('switch', { name: d.sql.testRunSample })).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(t.placeholders.write), 'Участки с истёкшей арендой{Enter}');
    await waitFor(() =>
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toEqual({
        content: 'Участки с истёкшей арендой',
        action: 'write',
        dialect: 'postgres',
        schema_id: null,
        test_run: false,
      }),
    );
    expect(server.callsTo('POST /api/dialogs')).toHaveLength(1);
    expect(document.documentElement.dataset.theme).toBe(theme);
  });

  it('выбор таблиц: снимок по схемам, имена из базы — текстом, отметки сохраняются одним запросом', async () => {
    const selection = { connection_id: 'c-1', title: CONNECTION.title, tables: [], available: true };
    const { server, user } = setup(`/sql/${ID}`, {
      [`GET /api/dialogs/${ID}`]: () => ok(dialog({ db: selection })),
      [`PATCH /api/dialogs/${ID}`]: (body) => ok(dialog({ db: { ...selection, ...(body as { db: object }).db } })),
    });
    await user.click(await screen.findByRole('button', { name: d.chooseTables }));

    const panel = await screen.findByText(d.picker.title);
    expect(panel).toBeInTheDocument();
    expect(await screen.findByText('cadastre')).toBeInTheDocument();
    expect(screen.getByText('<b>Правообладатели</b>')).toBeInTheDocument();
    expect(document.querySelector('b')).toBeNull();
    expect(screen.getByText(d.picker.noSample)).toBeInTheDocument();
    expect(screen.getByText(d.picker.checked(0, 2))).toBeInTheDocument();

    await user.click(screen.getByText('parcels'));
    await user.click(screen.getByText('owners'));
    await waitFor(() => expect(server.callsTo(`PATCH /api/dialogs/${ID}`)).toHaveLength(1));
    expect(server.callsTo(`PATCH /api/dialogs/${ID}`)[0]?.body).toEqual({
      db: {
        connection_id: 'c-1',
        tables: [
          { schema: 'cadastre', name: 'parcels' },
          { schema: 'cadastre', name: 'owners' },
        ],
      },
    });
    expect(screen.getByText(d.picker.checked(2, 2))).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: d.picker.close }));
    expect(await screen.findByRole('button', { name: d.tables(2) })).toBeInTheDocument();
  });

  it('подключение недоступно: заметка над лентой, перед вопросом выбор снимается у диалога', async () => {
    const gone = { connection_id: null, title: 'Архив ЕГРН', tables: [], available: false };
    const { server, user } = setup(`/sql/${ID}`, {
      [`GET /api/dialogs/${ID}`]: () => ok(dialog({ db: gone })),
      [`PATCH /api/dialogs/${ID}`]: () => ok(dialog({ db: null })),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['done', { status: 'complete' }]]),
    });
    expect(await screen.findByText(d.unavailableNotice('Архив ЕГРН'))).toBeInTheDocument();

    await user.type(screen.getByPlaceholderText(t.placeholders.write), 'Вопрос{Enter}');
    await waitFor(() => expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)).toHaveLength(1));
    expect(server.callsTo(`PATCH /api/dialogs/${ID}`)[0]?.body).toEqual({ db: null });
    expect(screen.queryByText(d.unavailableNotice('Архив ЕГРН'))).not.toBeInTheDocument();
  });

  it('«Проверить снова»: диалог занят до конца проверки, новый итог заменяет сохранённый', async () => {
    const saved: SqlRunBlock = {
      index: 0,
      line: 1,
      status: 'passed',
      reason: null,
      db: { connection_title: CONNECTION.title, sample_rows: 50, excluded_columns: [] },
      fixture: [{ table: 'cadastre.parcels', origin: 'sample', columns: ['id'], rows: null, truncated: false }],
      result: { columns: ['id'], rows: null, truncated: false, row_count: 3 },
      affected_rows: null,
      error: null,
      error_kind: null,
      fixed_sql: null,
    };
    const messages = [
      message({ id: 'u-1', role: 'user', content: 'Участки' }),
      message({ content: '```sql\nSELECT id FROM cadastre.parcels\n```', sql_run: { blocks: [saved] } }),
    ];
    const stream = liveSse();
    const { server, user } = setup(`/sql/${ID}`, {
      [`GET /api/dialogs/${ID}`]: () => ok(dialog({ title: 'Участки' })),
      'GET /api/dialogs': () => ok({ items: [dialog({ title: 'Участки' })], next_cursor: null }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [...messages].reverse(), next_cursor: null }),
      [`POST /api/dialogs/${ID}/messages/a-1/sql-run`]: () => stream.response,
    });
    expect(await screen.findByText(d.run.rowsCount(3))).toBeInTheDocument();
    expect(screen.getByText(d.run.notStored)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: d.run.recheck }));
    await waitFor(() => expect(server.callsTo(`POST /api/dialogs/${ID}/messages/a-1/sql-run`)[0]?.body).toEqual({ index: 0 }));
    // Диалог занят: вместо «Отправить» — «Остановить».
    expect(await screen.findByRole('button', { name: texts.chat.composer.stop })).toBeInTheDocument();

    stream.push(['sql_run', { blocks: [{ ...saved, result: { columns: ['id'], rows: [['1'], ['2']], truncated: false, row_count: 2 }, fixture: [{ ...saved.fixture?.[0], rows: [['1']] }] }] }], ['done', {}]);
    stream.close();
    expect(await screen.findByText(d.run.passed)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('button', { name: texts.chat.composer.stop })).not.toBeInTheDocument());
    expect(screen.queryByText(d.run.notStored)).not.toBeInTheDocument();
    expect(within(document.body).queryByRole('button', { name: d.run.recheck })).not.toBeInTheDocument();
  });
});
