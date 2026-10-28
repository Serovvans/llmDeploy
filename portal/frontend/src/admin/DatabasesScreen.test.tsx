import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { AdminDbConnection, PortalConfig } from '../api/types';
import { ADMIN, CONFIG, fail, mockApi, ok, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

const a = texts.db.admin;
const DB_CONFIG: PortalConfig = { ...CONFIG, db: { ...CONFIG.db, enabled: true } };

function connection(patch: Partial<AdminDbConnection> = {}): AdminDbConnection {
  return {
    id: 'c-1',
    title: 'Кадастр (рабочая)',
    description: 'Участки, договоры аренды, правообладатели',
    host: 'db.mio.local',
    port: 5432,
    database: 'cadastre',
    username: 'portal_reader',
    tls_mode: 'verify',
    all_schemas: false,
    schemas: ['cadastre'],
    samples_allowed: true,
    sample_exclusions: [{ schema: 'cadastre', table: 'owners', column: null }],
    access: { all: true },
    rights: {
      status: 'read_only',
      checked_at: '2026-10-25T11:05:00Z',
      findings: [],
      warnings: [{ kind: 'definer_function', privileges: ['EXECUTE'], objects: [{ type: 'function', name: 'cadastre.set_status' }], total: 1 }],
    },
    last_error: null,
    snapshot: { taken_at: '2026-10-25T11:05:00Z', tables: 214, truncated: false },
    refresh_available_at: null,
    created_at: '2026-10-25T09:00:00Z',
    updated_at: '2026-10-25T09:00:00Z',
    ...patch,
  };
}

const EXCESS = connection({
  id: 'c-2',
  title: 'Архив ЕГРН',
  description: '',
  host: '10.0.4.12',
  database: 'egrn',
  tls_mode: 'disable',
  samples_allowed: false,
  access: { all: false, users: [{ id: 'u-1', login: 'ivanov', full_name: 'Иванов Иван Иванович' }] },
  rights: {
    status: 'excess',
    checked_at: '2026-10-25T11:05:00Z',
    findings: [
      { kind: 'superuser', privileges: [], objects: [], total: 0 },
      { kind: 'write', privileges: ['INSERT'], objects: [{ type: 'table', name: 'egrn.rights' }], total: 1 },
    ],
    warnings: [],
  },
  snapshot: null,
});

function setup(handlers: Parameters<typeof mockApi>[0] = {}, config: PortalConfig = DB_CONFIG) {
  const server = mockApi({
    'GET /api/config': () => ok(config),
    'GET /api/auth/session': () => ok(session('ready', ADMIN)),
    'GET /api/admin/db-connections': () => ok({ items: [EXCESS, connection()] }),
    'GET /api/admin/users': () => ok({ items: [], page: 1, page_size: 50, total: 0 }),
    ...handlers,
  });
  renderApp('/admin/databases');
  return { server, user: userEvent.setup() };
}

async function row(title: string): Promise<HTMLElement> {
  return (await screen.findByText(title)).closest('tr') as HTMLElement;
}

describe('базы данных (администратор)', () => {
  it.each(['light', 'dark'] as const)('список в теме %s: «Управление», вкладки, итоги проверки словами', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    setup();

    expect(await screen.findByRole('heading', { level: 1, name: a.section })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: a.tabDatabases })).toHaveAttribute('href', '/admin/databases');
    expect(screen.getByText(a.privacy)).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(theme);
    expect(screen.getByRole('link', { name: a.section })).toHaveAttribute('aria-current', 'page');

    const ok = within(await row('Кадастр (рабочая)'));
    expect(ok.getByText('db.mio.local:5432/cadastre')).toBeInTheDocument();
    expect(ok.getByText(a.readOnly)).toBeInTheDocument();
    expect(ok.getByRole('button', { name: a.hasWarning })).toBeInTheDocument();
    expect(ok.getByText(a.exclusions(1))).toBeInTheDocument();
    expect(ok.getByText(a.snapshotTables(214))).toBeInTheDocument();

    const excess = within(await row('Архив ЕГРН'));
    expect(excess.getByText(a.noTls)).toBeInTheDocument();
    expect(excess.getByText(a.accessUsers(1))).toBeInTheDocument();
    expect(excess.getByText(a.noSnapshot)).toBeInTheDocument();
    // Предупреждение в счёт лишних прав не входит.
    expect(excess.getByRole('button', { name: a.excess(2) })).toBeInTheDocument();
  });

  it('«Лишние права» открывает форму на блоке «Проверка»: перечень, совет и пароль не показывается', async () => {
    const { user } = setup();
    await user.click(within(await row('Архив ЕГРН')).getByRole('button', { name: a.excess(2) }));

    expect(await screen.findByText(texts.db.rights.excess('portal_reader'))).toBeInTheDocument();
    expect(screen.getByText('SUPERUSER')).toBeInTheDocument();
    expect(screen.getByText('INSERT: egrn.rights')).toBeInTheDocument();
    expect(screen.getByText(texts.db.rights.copy)).toBeInTheDocument();
    expect(screen.getByText(texts.db.form.passwordSaved)).toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.getByRole('heading', { name: texts.db.form.check })).toHaveFocus();
  });

  it('новое подключение: ошибки формы — в браузере; «Сохранить и проверить» сохраняет и сразу проверяет', async () => {
    const created = connection({ id: 'c-3', title: 'Новая', rights: { status: 'unknown', checked_at: null, findings: [], warnings: [] }, snapshot: null });
    const checked = { ...created, rights: { status: 'read_only' as const, checked_at: '2026-10-26T09:00:00Z', findings: [], warnings: [] } };
    const { user, server } = setup({
      'POST /api/admin/db-connections': () => ok(created, 201),
      'POST /api/admin/db-connections/c-3/check': () => ok(checked),
    });
    await user.click(await screen.findByRole('button', { name: a.add }));
    await user.click(screen.getByRole('button', { name: texts.db.form.save }));
    expect(await screen.findByText(texts.db.form.errors.title)).toBeInTheDocument();
    expect(server.callsTo('POST /api/admin/db-connections')).toHaveLength(0);

    const inputs = () => screen.getAllByRole('textbox');
    await user.type(inputs()[0] as HTMLElement, 'Новая');
    await user.type(screen.getByPlaceholderText(texts.db.form.hostPlaceholder), 'db.local');
    await user.type(screen.getByLabelText(texts.db.form.database), 'cadastre');
    await user.type(screen.getByLabelText(texts.db.form.username), 'reader');
    await user.type(screen.getByLabelText(texts.db.form.password), 'secret');
    await user.type(screen.getByLabelText(texts.db.form.schemas), 'cadastre,');
    await user.click(screen.getByRole('button', { name: texts.db.form.save }));

    await waitFor(() => expect(server.callsTo('POST /api/admin/db-connections/c-3/check')).toHaveLength(1));
    expect(server.callsTo('POST /api/admin/db-connections')[0]?.body).toMatchObject({
      title: 'Новая',
      host: 'db.local',
      port: 5432,
      database: 'cadastre',
      username: 'reader',
      password: 'secret',
      tls_mode: 'verify',
      schemas: ['cadastre'],
      samples_allowed: false,
      access: { all: true },
    });
    expect(await screen.findByText(texts.db.rights.readOnly)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.db.rights.loadSnapshot })).toBeInTheDocument();
    // Итог проверки объявляется и принимает фокус (§9).
    await waitFor(() => expect(screen.getByText(texts.db.rights.readOnly).closest('[role="status"]')).toHaveFocus());
  });

  it('1.0.1: «все схемы» под адресом; схема не целиком — «не все» с пояснением по режиму и в сообщении об обновлении', async () => {
    const all = connection({ id: 'c-4', title: 'ГИС (вся база)', all_schemas: true, schemas: [], snapshot: { taken_at: '2026-10-28T09:12:00Z', tables: 3000, truncated: true } });
    const list = connection({ snapshot: { taken_at: '2026-10-28T09:12:00Z', tables: 2000, truncated: true } });
    const { user } = setup({
      'GET /api/admin/db-connections': () => ok({ items: [all, list] }),
      'POST /api/db/connections/c-4/snapshot': () => ok({ connection_id: 'c-4', taken_at: '2026-10-28T09:20:00Z', truncated: true, tables: [] }),
    });

    const allRow = within(await row('ГИС (вся база)'));
    expect(allRow.getByText(a.allSchemas)).toBeInTheDocument();
    const truncated = allRow.getByText(a.snapshotTables(3000, true));
    expect(truncated).toHaveTextContent('3000 таблиц — не все');
    await user.hover(truncated);
    expect(await screen.findByText(a.truncatedHint.all)).toBeInTheDocument();
    await user.unhover(truncated);

    const listRow = within(await row('Кадастр (рабочая)'));
    expect(listRow.queryByText(a.allSchemas)).not.toBeInTheDocument();
    await user.hover(listRow.getByText(a.snapshotTables(2000, true)));
    expect(await screen.findByText(a.truncatedHint.list)).toBeInTheDocument();

    await user.click(allRow.getByRole('button', { name: `${texts.users.actions}: ГИС (вся база)` }));
    await user.click(await screen.findByText(a.menu.refresh));
    expect(await screen.findByText('Схема обновлена: 0 таблиц — не все')).toBeInTheDocument();
  });

  it('1.0.1: новое подключение — «Только перечисленные»; при «Все схемы» список скрыт, его ошибка снята, `schemas` не уходит', async () => {
    const created = connection({ id: 'c-3', title: 'Новая', all_schemas: true, schemas: [], snapshot: null });
    const { user, server } = setup({
      'POST /api/admin/db-connections': () => ok(created, 201),
      'POST /api/admin/db-connections/c-3/check': () => ok(created),
    });
    await user.click(await screen.findByRole('button', { name: a.add }));
    const f = texts.db.form;
    expect(screen.getByRole('radio', { name: f.schemasModes.list })).toBeChecked();
    expect(screen.getByText(f.allSchemasNote)).toBeInTheDocument();

    await user.type(screen.getAllByRole('textbox')[0] as HTMLElement, 'Новая');
    await user.type(screen.getByPlaceholderText(f.hostPlaceholder), 'db.local');
    await user.type(screen.getByLabelText(f.database), 'gis');
    await user.type(screen.getByLabelText(f.username), 'reader');
    await user.type(screen.getByLabelText(f.password), 'secret');
    await user.click(screen.getByRole('button', { name: f.save }));
    expect(await screen.findByText(f.errors.schemas)).toBeInTheDocument();

    await user.click(screen.getByRole('radio', { name: new RegExp(f.schemasModes.all) }));
    expect(screen.queryByLabelText(f.schemas)).not.toBeInTheDocument();
    expect(screen.queryByText(f.errors.schemas)).not.toBeInTheDocument();
    expect(screen.getByRole('radio', { name: new RegExp(f.schemasModes.all) })).toHaveFocus();
    await user.click(screen.getByRole('button', { name: f.save }));

    await waitFor(() => expect(server.callsTo('POST /api/admin/db-connections')).toHaveLength(1));
    const body = server.callsTo('POST /api/admin/db-connections')[0]?.body as Record<string, unknown>;
    expect(body).toMatchObject({ all_schemas: true, database: 'gis' });
    expect(body).not.toHaveProperty('schemas');
  });

  it('1.0.1: смена режима у сохранённого — только изменённые поля; переход к списку — вместе со `schemas`', async () => {
    const all = connection({ id: 'c-4', title: 'ГИС (вся база)', all_schemas: true, schemas: [], snapshot: null });
    const { user, server } = setup({
      'GET /api/admin/db-connections': () => ok({ items: [all, connection()] }),
      'PATCH /api/admin/db-connections/c-1': () => ok(connection({ all_schemas: true, schemas: [] })),
      'POST /api/admin/db-connections/c-1/check': () => ok(connection({ all_schemas: true, schemas: [] })),
      'PATCH /api/admin/db-connections/c-4': () => ok({ ...all, all_schemas: false, schemas: ['cadastre'] }),
      'POST /api/admin/db-connections/c-4/check': () => ok({ ...all, all_schemas: false, schemas: ['cadastre'] }),
    });
    const f = texts.db.form;
    const edit = async (title: string) => {
      await user.click(within(await row(title)).getByRole('button', { name: `${texts.users.actions}: ${title}` }));
      await user.click(await screen.findByText(a.menu.edit));
    };

    await edit('Кадастр (рабочая)');
    await user.click(await screen.findByRole('radio', { name: new RegExp(f.schemasModes.all) }));
    await user.click(screen.getByRole('button', { name: f.save }));
    await waitFor(() => expect(server.callsTo('PATCH /api/admin/db-connections/c-1')).toHaveLength(1));
    expect(server.callsTo('PATCH /api/admin/db-connections/c-1')[0]?.body).toEqual({ all_schemas: true });
    await user.click(screen.getByRole('button', { name: texts.common.cancel }));

    await edit('ГИС (вся база)');
    expect(await screen.findByRole('radio', { name: new RegExp(f.schemasModes.all) })).toBeChecked();
    expect(screen.queryByLabelText(f.schemas)).not.toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: f.schemasModes.list }));
    await user.type(screen.getByLabelText(f.schemas), 'cadastre,');
    await user.click(screen.getByRole('button', { name: f.save }));
    await waitFor(() => expect(server.callsTo('PATCH /api/admin/db-connections/c-4')).toHaveLength(1));
    expect(server.callsTo('PATCH /api/admin/db-connections/c-4')[0]?.body).toEqual({ all_schemas: false, schemas: ['cadastre'] });
  });

  it('пусто — приглашение добавить; сбой — «Повторить»', async () => {
    let failing = true;
    setup({ 'GET /api/admin/db-connections': () => (failing ? fail(500, 'internal_error') : ok({ items: [] })) });
    const retry = await screen.findByRole('button', { name: texts.common.retry });
    failing = false;
    await userEvent.setup().click(retry);
    expect(await screen.findByText(a.empty.title)).toBeInTheDocument();
  });

  it('подключения выключены — экрана нет, рейка «Пользователи», адрес ведёт к пользователям', async () => {
    setup({}, CONFIG);
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/admin/users'));
    expect(screen.getByRole('link', { name: texts.nav.users })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: a.tabDatabases })).not.toBeInTheDocument();
  });
});
