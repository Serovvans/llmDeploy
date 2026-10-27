import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { CodeBuild, CodeEdit, CodeProject, CodeSdk, Message, PortalConfig } from '../api/types';
import { CONFIG, fail, mockApi, ok, session } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
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
  await screen.findByRole('heading', { level: 1, name: 'AddressEmailTool' });
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

  it('«Настройки» → Esc: фокус возвращается на кнопку, открывшую окно (§9)', async () => {
    const { user } = setup({ 'GET /api/sql/schemas': () => ok({ items: [] }) });
    await projectOpened();
    const opener = screen.getByRole('button', { name: w.project.settings });
    await user.click(opener);
    await screen.findByRole('button', { name: w.settings.save });

    await user.keyboard('{Escape}');
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it('«Действия» → «Удалить проект» → «Отмена»: фокус возвращается на «Действия» (§9)', async () => {
    const { user, server } = setup();
    await projectOpened();
    const opener = screen.getByRole('button', { name: `${texts.users.actions}: AddressEmailTool` });
    await user.click(opener);
    await user.click(await waitFor(() => menuItems().find((item) => item.textContent === w.removeProject) as HTMLElement));
    const modal = await screen.findByRole('dialog');
    await user.click(within(modal).getByRole('button', { name: texts.common.cancel }));

    await waitFor(() => expect(opener).toHaveFocus());
    expect(server.callsTo(`DELETE /api/code/projects/${ID}`)).toHaveLength(0);
  });

  it('приход правки объявляется; «Отклонить» без открытых файлов — фокус на карточку правки (§9)', async () => {
    const edit: CodeEdit = {
      status: 'proposed',
      files: [
        { path: 'A.cs', change: 'added', decision: null },
        { path: 'B.cs', change: 'added', decision: null },
      ],
      rejected_files: [],
      incomplete: false,
      verification: null,
      decided_at: null,
    };
    const base: Omit<Message, 'id' | 'role' | 'content' | 'created_at'> = {
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
    };
    const question: Message = { ...base, id: 'u-1', role: 'user', content: 'Добавь два файла', created_at: '2026-10-25T10:00:00Z' };
    const answer: Message = { ...base, id: 'a-1', role: 'assistant', content: 'Готово', code_edit: edit, created_at: '2026-10-25T10:01:00Z' };
    const { user, server } = setup({
      [`GET /api/code/projects/${ID}`]: () => ok(project({ files: [], build: null })),
      'GET /api/dialogs/d-1/messages': () => ok({ items: [answer, question], next_cursor: null }),
      [`GET /api/code/projects/${ID}/edits/a-1`]: () => fail(500, 'internal_error'),
      [`POST /api/code/projects/${ID}/edits/a-1/decision`]: () =>
        ok({ edit: { ...edit, status: 'rejected' }, project: project({ files: [], build: null }) }),
    });
    await projectOpened();

    expect(await screen.findByText(w.edit.announce(2))).toHaveAttribute('role', 'status');
    await user.click(screen.getByText(w.edit.reject));
    await waitFor(() => expect(server.callsTo(`POST /api/code/projects/${ID}/edits/a-1/decision`)).toHaveLength(1));
    await waitFor(() => expect(document.activeElement?.getAttribute('data-edit-card')).toBe('a-1'));
  });

  it('порядок списка проектов — по времени изменения: открытие не поднимает строку, новый файл поднимает (§5.13.3)', async () => {
    const item = (id: string, name: string, updatedAt: string) => ({ id, name, updated_at: updatedAt, build: null });
    const file = { id: 'f-2', path: 'Helpers.cs', content: '', revision: 1, updated_at: '2026-10-25T12:00:00Z' };
    let created = false;
    let listed = false;
    const { user } = setup({
      // The list is read once: what it shows afterwards is the screen's own bookkeeping.
      'GET /api/code/projects': () => {
        if (listed) {
          return new Promise(() => undefined);
        }
        listed = true;
        return ok({ items: [item('p-2', 'ParcelTool', '2026-10-25T11:00:00Z'), item(ID, 'AddressEmailTool', '2026-10-25T10:00:00Z')], next_cursor: null });
      },
      [`POST /api/code/projects/${ID}/files`]: () => {
        created = true;
        return ok(file, 201);
      },
      [`GET /api/code/projects/${ID}`]: () =>
        ok(created ? project({ files: [...project().files, file], updated_at: file.updated_at }) : project()),
    });
    const order = async () => {
      await user.click(screen.getByText(`← ${w.allProjects}`));
      const list = await screen.findByRole('complementary', { name: w.projects });
      return within(list).getAllByRole('listitem').map((row) => within(row).getByRole('link').textContent);
    };
    await projectOpened();

    expect((await order()).map((text) => text?.startsWith('ParcelTool'))).toEqual([true, false]);

    await user.click(screen.getByRole('link', { name: /AddressEmailTool/ }));
    await projectOpened();
    await user.click(screen.getByRole('button', { name: w.files.add }));
    await user.type(screen.getByLabelText(w.files.label), 'Helpers.cs{Enter}');
    await screen.findByRole('tab', { name: /Helpers\.cs/ });

    expect((await order()).map((text) => text?.startsWith('AddressEmailTool'))).toEqual([true, false]);
  });

  it('раздел выключен — адрес проекта ведёт на прежний экран помощника CoGIS', async () => {
    setup({ 'GET /api/dialogs': () => ok({ items: [], next_cursor: null }) }, CONFIG);
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/cogis'));
  });
});
