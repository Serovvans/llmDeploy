import { act, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { CodeProject, CodeWeb, Message, PortalConfig } from '../api/types';
import { CONFIG, fail, mockApi, ok, session, sse } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

// Monaco в jsdom не работает: редактор — заглушка, проверка синтаксиса — «ошибок нет», чтобы на строке
// «Путь до CoGIS» появилась кнопка «Исправить с помощником» у ошибок предпросмотра.
vi.mock('./monaco/loader', () => ({
  useMonaco: () => ({
    status: 'ready',
    setup: { configureWeb: () => Promise.resolve(), registerHtmlScriptHints: () => undefined, checkSyntax: () => Promise.resolve([]) },
  }),
  loadMonaco: () => new Promise(() => undefined),
}));
vi.mock('./CodeEditor', () => ({ CodeEditor: () => null }));

const ID = 'p-1';
const DIALOG = 'd-1';
const MESSAGES = `/api/dialogs/${DIALOG}/messages`;
const WEB_CONFIG: PortalConfig = { ...CONFIG, code: { ...CONFIG.code, enabled: true }, db: { ...CONFIG.db, enabled: true } };
const WEB: CodeWeb = {
  api_types: 'declare const api: {};',
  api_functions: ['getLayer'],
  libraries: [
    { id: 'jquery', version: '3.7.1', available: false },
    { id: 'knockout', version: '3.5.1', available: false },
    { id: 'leaflet', version: '1.9.4', available: false },
  ],
};
const PROJECT: CodeProject = {
  id: ID,
  name: 'Фильтр участков',
  kind: 'html_widget',
  packages: [],
  schema_id: null,
  db: { connection_id: 'c-1', title: 'Кадастр (рабочая)', tables: [{ schema: 'cadastre', name: 'parcels' }], available: true },
  dialog_id: DIALOG,
  files: [{ id: 'f-1', path: 'widget.html', content: '<div>Фильтр</div>\n<script>\nnull.x();\n</script>\n', revision: 1, updated_at: '2026-10-25T10:00:00Z' }],
  build: null,
  build_running: false,
  created_at: '2026-10-25T09:00:00Z',
  updated_at: '2026-10-25T10:00:00Z',
};
/** Текст вопроса, который собрал сервер (`header_sample`, концепция §5.13.20). */
const SERVER_TEXT =
  'Исправить ошибки, которые появились в предпросмотре. Слой был заполнен образцом из базы, поэтому текст ошибок не передан — только тип ошибки, файл и строка:\nwidget.html, строка 3: TypeError';

function message(id: string, role: Message['role'], content: string): Message {
  return {
    id,
    role,
    content,
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
    created_at: '2026-10-26T12:00:00Z',
  };
}

function setup() {
  let saved: Message[] = [];
  const server = mockApi({
    'GET /api/config': () => ok(WEB_CONFIG),
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/code/sdk': () => ok({ available: false, sdk_version: null, libraries: [], packages: [], types: [] }),
    'GET /api/code/web': () => ok(WEB),
    'GET /api/code/projects': () => ok({ items: [], next_cursor: null }),
    [`GET /api/code/projects/${ID}`]: () => ok(PROJECT),
    [`GET ${MESSAGES}`]: () => ok({ items: saved, next_cursor: null }),
    'GET /api/db/connections': () =>
      ok({
        items: [
          {
            id: 'c-1',
            title: 'Кадастр (рабочая)',
            description: '',
            usable: true,
            samples_allowed: true,
            snapshot: { taken_at: '2026-10-25T11:05:00Z', tables: 1, truncated: false },
            refresh_available_at: null,
          },
        ],
      }),
    [`GET /api/code/projects/${ID}/preview-layer`]: () =>
      ok({
        connection_title: 'Кадастр (рабочая)',
        schema: 'cadastre',
        table: 'parcels',
        rows: 1,
        truncated: false,
        excluded_columns: 0,
        geometry: null,
        features: { type: 'FeatureCollection', features: [{ type: 'Feature', geometry: null, properties: { owner: 'Иванов' } }] },
      }),
  });
  window.localStorage.setItem(`portal.codePreviewLayer.${ID}`, 'cadastre.parcels');
  renderApp(`/code/${ID}`);
  return { server, user: userEvent.setup(), save: (items: Message[]) => (saved = items) };
}

/** Запуск предпросмотра со слоем из образца и ошибка выполнения из фрейма. */
async function runWithError(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole('heading', { level: 1, name: PROJECT.name });
  await user.click(screen.getByRole('button', { name: texts.workshop.web.preview }));
  const frame = (await screen.findByTitle(/widget\.html/)) as HTMLIFrameElement;
  const target = frame.contentWindow as Window;
  const post = vi.spyOn(target, 'postMessage');
  await waitFor(() => {
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'ready' }, source: target }));
    });
    expect(post).toHaveBeenCalledTimes(1);
  });
  expect(post.mock.calls[0]?.[0]).toMatchObject({ layer: { source: 'sample' } });
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        data: { type: 'error', file: 'widget.html', line: 3, column: 1, message: "Uncaught TypeError: Cannot read properties of null (reading 'Иванов')" },
        source: target,
      }),
    );
  });
  return screen.findByRole('button', { name: texts.workshop.build.fix });
}

describe('«Исправить с помощником» по ошибкам предпросмотра (контракт §7.5.16, 0.25)', () => {
  it('слой из образца: тело — preview_fix без текста ошибки; лента показывает вопрос, собранный сервером', async () => {
    const { server, user, save } = setup();
    server.on(`POST ${MESSAGES}`, () => {
      save([message('m-q', 'user', SERVER_TEXT), message('m-a', 'assistant', 'Исправляю.')]);
      return sse([
        ['start', { user_message_id: 'm-q', assistant_message_id: 'm-a' }],
        ['delta', { text: 'Исправляю.' }],
        ['done', { status: 'complete' }],
      ]);
    });

    await user.click(await runWithError(user));

    await waitFor(() => expect(server.callsTo(`POST ${MESSAGES}`)).toHaveLength(1));
    const body = server.callsTo(`POST ${MESSAGES}`)[0]?.body as Record<string, unknown>;
    expect(body).toEqual({
      action: 'write',
      file_ids: null,
      target: null,
      preview_fix: { layer: 'sample', problems: [{ kind: 'error', path: 'widget.html', line: 3, type: 'TypeError' }] },
    });
    expect(JSON.stringify(body)).not.toContain('Иванов');

    // Действие `questionSaved`: после `start` вопрос в ленте — текст сервера.
    expect(await screen.findByText(/Слой был заполнен образцом из базы/)).toBeInTheDocument();
    expect(screen.getByText(/widget\.html, строка 3: TypeError/)).toBeInTheDocument();
  });

  it('сервер отказал по файлу (код изменился после запуска) — уведомление, вопроса в ленте нет', async () => {
    const { server, user } = setup();
    server.on(`POST ${MESSAGES}`, () => fail(422, 'validation_error', { fields: [{ field: 'preview_fix', code: 'unknown_value', message: '' }] }));

    await user.click(await runWithError(user));

    expect(await screen.findByText(texts.workshop.web.pane.fixChanged)).toBeInTheDocument();
    expect(screen.queryByText(/Слой был заполнен образцом из базы/)).toBeNull();
  });
});
