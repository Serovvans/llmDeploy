import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { CodeBuild, CodeProject, CodeSdk, PortalConfig } from '../api/types';
import { CONFIG, fail, mockApi, ok, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

// Monaco в jsdom не работает: экран проверяется без редактора — он остаётся в «Загрузка редактора…».
vi.mock('./monaco/loader', () => ({
  useMonaco: () => ({ status: 'loading' }),
  loadMonaco: () => new Promise(() => undefined),
}));

const w = texts.workshop;
const ID = 'p-1';
const CODE_CONFIG: PortalConfig = { ...CONFIG, code: { ...CONFIG.code, enabled: true } };
const SDK: CodeSdk = { available: true, sdk_version: '13.2.0', libraries: [], packages: [], types: [] };

function build(patch: Partial<CodeBuild> = {}): CodeBuild {
  return {
    status: 'failed',
    outdated: false,
    sdk_version: '13.2.0',
    packages: [],
    diagnostics: [
      { severity: 'error', code: 'CS1002', file: 'Tool.cs', line: 5, column: 14, end_line: 5, end_column: 15, message: 'Требуется ";".' },
    ],
    diagnostics_truncated: false,
    factory: null,
    tools: null,
    findings: null,
    archive: null,
    created_at: '2026-10-25T10:00:00Z',
    ...patch,
  };
}

function project(patch: Partial<CodeProject> = {}): CodeProject {
  return {
    id: ID,
    name: 'AddressEmailTool',
    packages: [],
    schema_id: null,
    dialog_id: 'd-1',
    files: [{ id: 'f-1', path: 'Tool.cs', content: 'using Npgsql\n', revision: 1, updated_at: '2026-10-25T10:00:00Z' }],
    build: build(),
    build_running: false,
    created_at: '2026-10-25T09:00:00Z',
    updated_at: '2026-10-25T10:00:00Z',
    ...patch,
  };
}

function setup(handlers: Parameters<typeof mockApi>[0] = {}, config: PortalConfig = CODE_CONFIG) {
  const server = mockApi({
    'GET /api/config': () => ok(config),
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/code/sdk': () => ok(SDK),
    'GET /api/code/projects': () => ok({ items: [], next_cursor: null }),
    [`GET /api/code/projects/${ID}`]: () => ok(project()),
    'GET /api/dialogs/d-1/messages': () => ok({ items: [], next_cursor: null }),
    ...handlers,
  });
  renderApp(`/code/${ID}`);
  return { server, user: userEvent.setup() };
}

/**
 * Экран открыт: раздел грузится лениво (`lazy`), и первая отрисовка под параллельной нагрузкой дольше
 * обычной секунды ожидания — предел как у других тестов экранов с ленивой частью.
 */
async function projectOpened(): Promise<void> {
  await screen.findByRole('heading', { level: 1, name: 'AddressEmailTool' }, { timeout: 5000 });
}

function route(): HTMLElement {
  return screen.getByRole('list', { name: w.build.route });
}

describe('раздел «Код»: экран проекта', () => {
  it.each(['light', 'dark'] as const)('проект с ошибкой сборки: станция, «Исправить с помощником», число у файла (тема %s)', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    setup();

    await projectOpened();
    expect(within(route()).getByText(w.build.failed(1))).toBeInTheDocument();
    expect(within(route()).getByRole('button', { name: w.build.fix })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: w.build.build })).toBeInTheDocument();
    // Ошибка сборки у файла в дереве.
    expect(screen.getByLabelText(/1 ошибка сборки/)).toBeInTheDocument();
    // Колонка помощника открыта при первом входе, редактор — в загрузке.
    expect(screen.getByRole('heading', { level: 2, name: w.assistant.title })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(theme);
  });

  it('«Собрать» → 503: станция «Сборка недоступна», без отдельного уведомления', async () => {
    const { user, server } = setup({ [`POST /api/code/projects/${ID}/build`]: () => fail(503, 'build_unavailable') });
    await projectOpened();
    await user.click(screen.getByRole('button', { name: w.build.build }));

    expect(await within(route()).findByText(w.build.unavailable)).toBeInTheDocument();
    expect(server.callsTo(`POST /api/code/projects/${ID}/build`)).toHaveLength(1);
  });

  it('«Собрать» → 429: проект перечитывается; идёт чужая сборка — «Собираю…»', async () => {
    let running = false;
    const { user } = setup({
      [`GET /api/code/projects/${ID}`]: () => ok(project({ build_running: running })),
      [`POST /api/code/projects/${ID}/build`]: () => {
        running = true;
        return fail(429, 'build_in_progress');
      },
    });
    await projectOpened();
    await user.click(screen.getByRole('button', { name: w.build.build }));

    expect(await within(route()).findByText(w.build.building)).toBeInTheDocument();
  });

  it('сохранённое изменение файлов — оценка памяти повторяется через 300 мс (правило 7)', async () => {
    const memory = { level: 'free', percent: 5, used_tokens: 900, limit_tokens: 28500, compress_pairs: null, pairs: [] };
    const file = { id: 'f-2', path: 'Helpers.cs', content: '', revision: 1, updated_at: '2026-10-25T10:01:00Z' };
    const { user, server } = setup(
      {
        'GET /api/dialogs/d-1/memory': () => ok(memory),
        [`POST /api/code/projects/${ID}/files`]: () => ok(file, 201),
      },
      { ...CODE_CONFIG, memory: { ...CODE_CONFIG.memory, enabled: true } },
    );
    await projectOpened();
    await waitFor(() => expect(server.callsTo('GET /api/dialogs/d-1/memory')).toHaveLength(1));

    await user.click(screen.getByRole('button', { name: w.files.add }));
    await user.type(screen.getByLabelText(w.files.label), 'Helpers.cs{Enter}');

    await waitFor(() => expect(server.callsTo(`POST /api/code/projects/${ID}/files`)).toHaveLength(1));
    await waitFor(() => expect(server.callsTo('GET /api/dialogs/d-1/memory')).toHaveLength(2), { timeout: 2000 });
  });

  it('раздел выключен — адрес проекта ведёт на прежний экран помощника CoGIS', async () => {
    setup({ 'GET /api/dialogs': () => ok({ items: [], next_cursor: null }) }, CONFIG);
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/cogis'), { timeout: 5000 });
  });
});
