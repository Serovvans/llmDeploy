import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { CodeProject, CodeWeb, PortalConfig } from '../api/types';
import { CONFIG, mockApi, ok, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

// Monaco в jsdom не работает: экран проверяется без редактора.
vi.mock('./monaco/loader', () => ({
  useMonaco: () => ({ status: 'loading' }),
  loadMonaco: () => new Promise(() => undefined),
}));

const w = texts.workshop;
const ID = 'p-1';
const WEB_CONFIG: PortalConfig = { ...CONFIG, code: { ...CONFIG.code, enabled: true }, db: { ...CONFIG.db, enabled: true } };
const WEB: CodeWeb = {
  api_types: 'declare const api: { showAlert(text: string): void };',
  api_functions: ['showAlert', 'getLayer'],
  libraries: [
    { id: 'jquery', version: '3.7.1', available: false },
    { id: 'knockout', version: '3.5.1', available: false },
    { id: 'leaflet', version: '1.9.4', available: false },
  ],
};

function project(patch: Partial<CodeProject> = {}): CodeProject {
  return {
    id: ID,
    name: 'Фильтр участков',
    kind: 'html_widget',
    packages: [],
    schema_id: null,
    db: null,
    dialog_id: 'd-1',
    files: [{ id: 'f-1', path: 'widget.html', content: '<div>Фильтр</div>\n', revision: 1, updated_at: '2026-10-25T10:00:00Z' }],
    build: null,
    build_running: false,
    created_at: '2026-10-25T09:00:00Z',
    updated_at: '2026-10-25T10:00:00Z',
    ...patch,
  };
}

function setup(handlers: Parameters<typeof mockApi>[0] = {}) {
  const server = mockApi({
    'GET /api/config': () => ok(WEB_CONFIG),
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/code/sdk': () => ok({ available: false, sdk_version: null, libraries: [], packages: [], types: [] }),
    'GET /api/code/web': () => ok(WEB),
    'GET /api/code/projects': () => ok({ items: [], next_cursor: null }),
    [`GET /api/code/projects/${ID}`]: () => ok(project()),
    'GET /api/dialogs/d-1/messages': () => ok({ items: [], next_cursor: null }),
    'GET /api/sql/schemas': () => ok({ items: [{ id: 's-1', name: 'Аренда', updated_at: '2026-10-01T09:00:00Z' }] }),
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
    'GET /api/db/connections/c-1/snapshot': () =>
      ok({
        connection_id: 'c-1',
        taken_at: '2026-10-25T11:05:00Z',
        truncated: false,
        tables: [{ schema: 'cadastre', name: 'parcels', kind: 'table', comment: null, samples_excluded: false, columns: [] }],
      }),
    ...handlers,
  });
  renderApp(`/code/${ID}`);
  return { server, user: userEvent.setup() };
}

async function opened(): Promise<void> {
  await screen.findByRole('heading', { level: 1, name: 'Фильтр участков' }, { timeout: 5000 });
}

describe('раздел «Код» 1.0: предпросмотр', () => {
  it('фрейм изолирован: sandbox только со скриптами, без referrer; сообщения не от фрейма отбрасываются', async () => {
    const { user } = setup();
    await opened();
    await user.click(screen.getByRole('button', { name: w.web.preview }));

    const frame = (await screen.findByTitle(/widget\.html/)) as HTMLIFrameElement;
    // Без allow-same-origin код во фрейме — в уникальном источнике: нет cookie, хранилища и API портала (§7.5.17).
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts');
    expect(frame.getAttribute('src')).toBe('/code-preview/frame.html');
    expect(frame.getAttribute('referrerpolicy')).toBe('no-referrer');

    const target = frame.contentWindow as Window;
    const post = vi.spyOn(target, 'postMessage');
    // Чужое окно: «ready» не принимается, код не уходит.
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'ready' }, source: window }));
    });
    expect(post).not.toHaveBeenCalled();

    await waitFor(() => {
      act(() => {
        window.dispatchEvent(new MessageEvent('message', { data: { type: 'ready' }, source: target }));
      });
      expect(post).toHaveBeenCalledTimes(1);
    });
    const [init, origin] = post.mock.calls[0] ?? [];
    expect(origin).toBe('*');
    expect(init).toMatchObject({ type: 'init', kind: 'html_widget', files: [{ path: 'widget.html' }], api_functions: WEB.api_functions });
    // Второй «ready» код не переотправляет.
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'ready' }, source: target }));
    });
    expect(post).toHaveBeenCalledTimes(1);

    // Ошибка из фрейма — текстом; неизвестный тип — отброшен.
    act(() => {
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'log', fn: 'showAlert', args: ['<img src=x>'], known: true, count: null }, source: target }));
      window.dispatchEvent(new MessageEvent('message', { data: { type: 'html', html: '<b>x</b>' }, source: target }));
    });
    expect(await screen.findByText(/<img src=x>/)).toBeInTheDocument();
    expect(document.querySelector('#preview-pane img, #preview-pane b')).toBeNull();
  });
});

describe('раздел «Код» 1.0: настройки проекта', () => {
  it.each(['light', 'dark'] as const)('вид — только чтение; подключение и таблицы — одним сохранением (тема %s)', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    const { server, user } = setup({
      [`PATCH /api/code/projects/${ID}`]: () => ok(project()),
    });
    await opened();
    await user.click(screen.getByRole('button', { name: w.project.settings }));

    const modal = await screen.findByText(w.settings.title);
    expect(modal).toBeInTheDocument();
    const kind = w.web.kinds.html_widget;
    expect(screen.getByText(w.web.settings.kindValue(kind.title, kind.where))).toBeInTheDocument();
    // У видов JS и HTML нет имён для CoGIS и пакетов.
    expect(screen.queryByText(w.settings.names)).not.toBeInTheDocument();
    expect(screen.queryByText(w.settings.packages)).not.toBeInTheDocument();
    expect(screen.getByText(w.web.settings.schemasNoteDb)).toBeInTheDocument();

    await user.click(screen.getByText(w.settings.noSchema));
    await user.click(await screen.findByText('Кадастр (рабочая)'));
    await user.click(await screen.findByText('parcels'));
    await user.click(screen.getByRole('button', { name: w.settings.save }));

    await waitFor(() => expect(server.callsTo(`PATCH /api/code/projects/${ID}`)).toHaveLength(1));
    expect(server.callsTo(`PATCH /api/code/projects/${ID}`)[0]?.body).toEqual({
      db: { connection_id: 'c-1', tables: [{ schema: 'cadastre', name: 'parcels' }] },
    });
    expect(document.documentElement.dataset.theme).toBe(theme);
  });

  it('«Без схемы» снимает и схему, и подключение; Каталог — хотя бы один тип клиента', async () => {
    const catalog = project({
      kind: 'catalog',
      schema_id: 's-1',
      files: [{ id: 'f-1', path: 'style.css', content: '', revision: 1, updated_at: '2026-10-25T10:00:00Z', catalog: null }],
    });
    const { server, user } = setup({
      [`GET /api/code/projects/${ID}`]: () => ok(catalog),
      [`PATCH /api/code/projects/${ID}`]: () => ok(catalog),
    });
    await opened();
    await user.click(screen.getByRole('button', { name: w.project.settings }));
    await screen.findByText(w.web.settings.catalog);

    for (const client of ['smartphone', 'tablet', 'desktop'] as const) {
      await user.click(screen.getByText(w.web.settings.clients[client]));
    }
    await user.click(screen.getByRole('button', { name: w.settings.save }));
    expect(await screen.findByText(w.web.settings.noClient)).toBeInTheDocument();
    expect(server.callsTo(`PATCH /api/code/projects/${ID}`)).toHaveLength(0);

    await user.click(screen.getByText(w.web.settings.clients.desktop));
    await user.click(screen.getByText('Аренда'));
    await user.click(await screen.findByText(w.settings.noSchema, { selector: '[data-tid="MenuItem__root"] *' }));
    await user.click(screen.getByRole('button', { name: w.settings.save }));
    await waitFor(() => expect(server.callsTo(`PATCH /api/code/projects/${ID}`)).toHaveLength(1));
    expect(server.callsTo(`PATCH /api/code/projects/${ID}`)[0]?.body).toEqual({
      schema_id: null,
      db: null,
      catalog_files: [{ file_id: 'f-1', insertion: 'head_end', clients: ['desktop'] }],
    });
    expect(within(document.body).queryByText(w.web.settings.noClient)).not.toBeInTheDocument();
  });
});
