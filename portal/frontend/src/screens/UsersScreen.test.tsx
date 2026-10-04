import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { AdminUser } from '../api/types';
import { ADMIN, fail, mockApi, ok, session } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { texts } from '../texts';

const t = texts.users;

function adminUser(overrides: Partial<AdminUser>): AdminUser {
  return {
    id: 'u-1',
    login: 'ivanov',
    full_name: 'Иванов Иван Иванович',
    role: 'employee',
    state: 'active',
    second_factor_configured: true,
    is_me: false,
    created_at: '2026-10-04T09:00:00Z',
    ...overrides,
  };
}

const USERS = [
  adminUser({}),
  adminUser({ id: 'u-2', login: 'petrova', full_name: 'Петрова Анна Сергеевна', state: 'never_logged_in', second_factor_configured: false }),
  adminUser({ id: 'u-0', login: 'serov', full_name: 'Серов Иван', role: 'admin', is_me: true }),
  adminUser({ id: 'u-3', login: 'sidorov', full_name: 'Сидоров Олег Петрович', state: 'blocked' }),
];

function page(items: AdminUser[] = USERS) {
  return ok({ items, page: 1, page_size: 50, total: items.length });
}

function setup(theme?: 'light' | 'dark') {
  if (theme) {
    window.localStorage.setItem('portal.theme', theme);
  }
  const server = mockApi({
    'GET /api/auth/session': () => ok(session('ready', ADMIN)),
    'GET /api/admin/users': () => page(),
  });
  renderApp('/admin/users');
  return { server, user: userEvent.setup() };
}

async function row(name: string): Promise<HTMLElement> {
  return (await screen.findByText(name)).closest('tr') as HTMLElement;
}

async function openMenu(user: ReturnType<typeof userEvent.setup>, name: string, item: string) {
  await user.click(within(await row(name)).getByRole('button', { name: `${t.actions}: ${name}` }));
  await user.click(await screen.findByText(item));
}

describe('пользователи', () => {
  it.each(['light', 'dark'] as const)('список в теме %s: значения словами, в своей строке действий нет', async (theme) => {
    setup(theme);

    expect(await screen.findByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(theme);
    expect(screen.getByText(t.privacy)).toBeInTheDocument();

    const petrova = within(await row('Петрова Анна Сергеевна'));
    expect(petrova.getByText('Сотрудник')).toBeInTheDocument();
    expect(petrova.getByText('Не настроен')).toBeInTheDocument();
    expect(petrova.getByText('Первый вход не завершён')).toBeInTheDocument();
    expect(within(await row('Иванов Иван Иванович')).getByText('Активна')).toBeInTheDocument();
    expect(within(await row('Сидоров Олег Петрович')).getByText('Заблокирована')).toBeInTheDocument();

    const me = within(await row('Серов Иван'));
    expect(me.getByText('(вы)')).toBeInTheDocument();
    expect(me.getByText('Администратор')).toBeInTheDocument();
    expect(me.queryByRole('button')).not.toBeInTheDocument();
  });

  it('набор действий зависит от строки: сброс второго фактора — только если настроен; блокировка или разблокировка', async () => {
    const { user } = setup();

    await user.click(within(await row('Петрова Анна Сергеевна')).getByRole('button'));
    await screen.findByText(t.menu.edit);
    expect(menuItems().map((item) => item.textContent)).toEqual(['Изменить', 'Сбросить пароль', 'Заблокировать']);
    await user.keyboard('{Escape}');

    await user.click(within(await row('Сидоров Олег Петрович')).getByRole('button'));
    await waitFor(() =>
      expect(menuItems().map((item) => item.textContent)).toEqual([
        'Изменить',
        'Сбросить пароль',
        'Сбросить второй фактор',
        'Разблокировать',
      ]),
    );
  });

  it('поиск и сортировка уходят на сервер; пустой поиск — «Никого не нашлось» со сбросом', async () => {
    const { server, user } = setup();
    await row('Иванов Иван Иванович');

    await user.click(screen.getByRole('button', { name: t.columns.fullName }));
    await waitFor(() => expect(screen.getByRole('columnheader', { name: t.columns.fullName })).toHaveAttribute('aria-sort', 'descending'));

    server.on('GET /api/admin/users', (_body, url) => (url.searchParams.get('q') ? page([]) : page()));
    await user.type(screen.getByLabelText(t.search), 'яяя');
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    expect(screen.getByText(t.empty.text)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: t.empty.action }));
    expect(await screen.findByText('Иванов Иван Иванович')).toBeInTheDocument();
    expect(screen.getByLabelText(t.search)).toHaveValue('');
  });

  it('создание: ошибки полей, затем временный пароль в том же окне', async () => {
    const { server, user } = setup();
    await user.click(await screen.findByRole('button', { name: t.add }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByText(t.form.createTitle)).toBeInTheDocument();
    expect(dialog.getByText(t.form.loginHint)).toBeInTheDocument();

    await user.click(dialog.getByRole('button', { name: t.form.create }));
    expect(dialog.getByText(t.form.enterFullName)).toBeInTheDocument();
    expect(dialog.getByText(t.form.enterLogin)).toBeInTheDocument();

    await user.type(dialog.getByLabelText(t.form.fullNameLabel), 'Петрова Анна Сергеевна');
    await user.type(dialog.getByLabelText(t.form.loginLabel), 'Petrova');

    server.on('POST /api/admin/users', () => fail(409, 'login_taken'));
    await user.click(dialog.getByRole('button', { name: t.form.create }));
    expect(await dialog.findByText(t.form.loginTaken)).toBeInTheDocument();

    server.on('POST /api/admin/users', () =>
      fail(422, 'validation_error', { fields: [{ field: 'login', code: 'invalid_format', message: 'Текст сервера.' }] }),
    );
    await user.click(dialog.getByRole('button', { name: t.form.create }));
    expect(await dialog.findByText(t.form.loginInvalid)).toBeInTheDocument();

    server.on('POST /api/admin/users', () =>
      ok({ user: adminUser({ id: 'u-9', login: 'petrova', state: 'never_logged_in' }), temporary_password: 'Kf7-pQ2x-Wm94' }, 201),
    );
    await user.click(dialog.getByRole('button', { name: t.form.create }));

    const result = within(await screen.findByRole('dialog'));
    expect(await result.findByText(t.temporaryPassword.createdTitle)).toBeInTheDocument();
    expect(result.getByText('Kf7-pQ2x-Wm94')).toBeInTheDocument();
    expect(result.getByText('petrova')).toBeInTheDocument();
    expect(server.callsTo('POST /api/admin/users').at(-1)?.body).toEqual({
      full_name: 'Петрова Анна Сергеевна',
      login: 'Petrova',
      role: 'employee',
    });

    await user.click(result.getByRole('button', { name: t.temporaryPassword.done }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('изменение: без правок окно просто закрывается; только ФИО — сразу; роль — после подтверждения', async () => {
    const { server, user } = setup();
    server.on('PATCH /api/admin/users/u-1', () => ok(adminUser({})));

    await openMenu(user, 'Иванов Иван Иванович', t.menu.edit);
    let dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByText(t.form.loginReadonly)).toBeInTheDocument();
    await user.click(dialog.getByRole('button', { name: t.form.save }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(server.callsTo('PATCH /api/admin/users/u-1')).toHaveLength(0);

    await openMenu(user, 'Иванов Иван Иванович', t.menu.edit);
    dialog = within(await screen.findByRole('dialog'));
    await user.type(dialog.getByLabelText(t.form.fullNameLabel), '-Петров');
    await user.click(dialog.getByRole('button', { name: t.form.save }));
    expect(await screen.findByText(t.toast.updated)).toBeInTheDocument();
    expect(server.callsTo('PATCH /api/admin/users/u-1')[0]?.body).toEqual({ full_name: 'Иванов Иван Иванович-Петров' });

    await openMenu(user, 'Иванов Иван Иванович', t.menu.edit);
    dialog = within(await screen.findByRole('dialog'));
    await user.click(dialog.getByLabelText(t.form.roleAdmin));
    await user.click(dialog.getByRole('button', { name: t.form.save }));
    expect(await screen.findByText('Сменить роль на „Администратор“?')).toBeInTheDocument();
    expect(screen.getByText('Иванов Иван Иванович (ivanov)')).toBeInTheDocument();
    expect(screen.getByText(t.confirm.changeRole('admin').body)).toBeInTheDocument();
    expect(server.callsTo('PATCH /api/admin/users/u-1')).toHaveLength(1);

    await user.click(screen.getByRole('button', { name: 'Сменить роль' }));
    expect(await screen.findByText(t.toast.roleChanged)).toBeInTheDocument();
    expect(server.callsTo('PATCH /api/admin/users/u-1')[1]?.body).toEqual({ role: 'admin' });
  });

  it('сброс пароля: подтверждение с фокусом на «Отмена», затем окно «Пароль сброшен»', async () => {
    const { server, user } = setup();
    server.on('POST /api/admin/users/u-1/reset-password', () =>
      ok({ user: adminUser({}), temporary_password: 'Zx1-aB2c-De34' }),
    );

    await openMenu(user, 'Иванов Иван Иванович', t.menu.resetPassword);
    expect(await screen.findByText('Сбросить пароль?')).toBeInTheDocument();
    expect(screen.getByText('Иванов Иван Иванович (ivanov)')).toBeInTheDocument();
    expect(screen.getByText(t.confirm.resetPassword.body)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: texts.common.cancel })).toHaveFocus());

    await user.click(screen.getByRole('button', { name: 'Сбросить' }));
    expect(await screen.findByText(t.temporaryPassword.resetTitle)).toBeInTheDocument();
    expect(screen.getByText('Zx1-aB2c-De34')).toBeInTheDocument();
  });

  it('блокировка — с подтверждением, разблокировка — без; после действия список перезапрашивается', async () => {
    const { server, user } = setup();
    server.on('POST /api/admin/users/u-1/block', () => ok(adminUser({ state: 'blocked' })));
    server.on('POST /api/admin/users/u-3/unblock', () => ok(adminUser({ id: 'u-3' })));
    await row('Иванов Иван Иванович');
    const listCalls = () => server.callsTo('GET /api/admin/users').length;

    let before = listCalls();
    await openMenu(user, 'Иванов Иван Иванович', t.menu.block);
    expect(await screen.findByText('Заблокировать пользователя?')).toBeInTheDocument();
    expect(screen.getByText('Иванов Иван Иванович (ivanov)')).toBeInTheDocument();
    expect(server.callsTo('POST /api/admin/users/u-1/block')).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: 'Заблокировать' }));
    expect(await screen.findByText(t.toast.blocked)).toBeInTheDocument();
    await waitFor(() => expect(listCalls()).toBe(before + 1));

    before = listCalls();
    await openMenu(user, 'Сидоров Олег Петрович', t.menu.unblock);
    expect(await screen.findByText(t.toast.unblocked)).toBeInTheDocument();
    await waitFor(() => expect(listCalls()).toBe(before + 1));
  });

  it.each([
    ['cannot_modify_self', t.toast.cannotModifySelf],
    ['internal_error', texts.common.actionFailed],
  ])('отказ %s при сбросе второго фактора — сообщение с ошибкой', async (code, text) => {
    const { server, user } = setup();
    server.on('POST /api/admin/users/u-1/reset-second-factor', () => fail(409, code));

    await openMenu(user, 'Иванов Иван Иванович', t.menu.resetSecondFactor);
    expect(await screen.findByText('Сбросить второй фактор?')).toBeInTheDocument();
    expect(screen.getByText(t.confirm.resetSecondFactor.body)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Сбросить' }));
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it('список не загрузился — заметка с «Повторить»', async () => {
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready', ADMIN)),
      'GET /api/admin/users': () => fail(503, 'service_unavailable'),
    });
    renderApp('/admin/users');

    expect(await screen.findByRole('alert')).toHaveTextContent(texts.common.serverFailure);
    server.on('GET /api/admin/users', () => page());
    await userEvent.setup().click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByText('Иванов Иван Иванович')).toBeInTheDocument();
  });
});
