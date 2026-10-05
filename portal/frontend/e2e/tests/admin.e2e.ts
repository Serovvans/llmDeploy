/**
 * Критерий 2 (docs/portal-design.md §10): администратор создаёт и блокирует пользователя,
 * сбрасывает пароль и второй фактор; заблокированный теряет доступ сразу.
 */
import type { Page } from '@playwright/test';

import { lockLogin, newApi, nextCode, signIn, uniqueSuffix, usedCode } from '../support/accounts';
import { expect, test, type Member } from '../support/fixtures';
import { createDialog, eventsOf, expectError, parseStream } from '../support/portal';
import { fillLogin, searchUsers, toast } from '../support/ui';

/** Открывает меню действий в строке пользователя, найдя его поиском по логину. */
async function openUserMenu(page: Page, member: Member): Promise<void> {
  await searchUsers(page, member.account.login);
  await expect(page.getByRole('row').filter({ hasText: member.account.login })).toHaveCount(1);
  await page.getByRole('button', { name: `Действия: ${member.account.fullName}` }).click();
}

function menuItem(page: Page, name: string) {
  return page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: new RegExp(`^${name}$`) });
}

test('администратор создаёт пользователя, тот входит с временным паролем', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/users');
  const login = `e2e-ui-${uniqueSuffix()}`;
  const fullName = `Петрова Анна ${login}`;

  await page.getByRole('button', { name: 'Добавить пользователя' }).click();
  await expect(page.getByText('Новый пользователь')).toBeVisible();
  await page.getByRole('textbox', { name: 'Фамилия, имя, отчество' }).fill(fullName);
  await page.getByRole('textbox', { name: 'Логин', exact: true }).fill(login);
  await page.getByRole('button', { name: 'Создать' }).click();

  await expect(page.getByText('Учётная запись создана')).toBeVisible();
  const dialog = page.getByRole('dialog');
  await expect(dialog).toContainText(login);
  const temporaryPassword = (await dialog.locator('.p-mono').last().innerText()).trim();
  expect(temporaryPassword.length).toBeGreaterThanOrEqual(12);
  await page.getByRole('button', { name: 'Готово' }).click();

  await searchUsers(page, login);
  const row = page.getByRole('row').filter({ hasText: login });
  await expect(row).toContainText('Сотрудник');
  await expect(row).toContainText('Не настроен');
  await expect(row).toContainText('Первый вход не завершён');

  const api = await newApi();
  const session = await api.post('/api/auth/login', { data: { login, password: temporaryPassword } });
  expect(session.status()).toBe(200);
  expect((await session.json()).step).toBe('password_change');
  await api.dispose();
});

test('занятый и негодный логин отклоняются с понятным текстом', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/users');
  await page.getByRole('button', { name: 'Добавить пользователя' }).click();
  await page.getByRole('textbox', { name: 'Фамилия, имя, отчество' }).fill('Иванов Иван Иванович');

  await page.getByRole('textbox', { name: 'Логин', exact: true }).fill('Иванов');
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByRole('alert')).toContainText('В логине от 3 до 32 символов');

  await page.getByRole('textbox', { name: 'Логин', exact: true }).fill(admin.account.login);
  await page.getByRole('button', { name: 'Создать' }).click();
  await expect(page.getByRole('alert')).toHaveText('Такой логин уже занят');
});

test('блокировка: доступ пропадает сразу, вход закрыт; разблокировка возвращает вход', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const employee = await newMember();
  const employeePage = await pageAs(employee, '/chat');
  await expect(employeePage.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

  const page = await pageAs(admin, '/admin/users');
  await openUserMenu(page, employee);
  await menuItem(page, 'Заблокировать').click();
  await expect(page.getByText('Заблокировать пользователя?')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Заблокировать' }).click();
  await expect(toast(page, 'Пользователь заблокирован')).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: employee.account.login })).toContainText('Заблокирована');

  // Открытая сессия сотрудника перестала действовать без ожидания.
  await expectError(await employee.api.get('/api/auth/session'), 401, 'unauthenticated');
  await expectError(await employee.api.get('/api/dialogs?kind=chat'), 401, 'unauthenticated');
  await employeePage.getByRole('link', { name: 'База знаний' }).click();
  await expect(employeePage).toHaveURL(/\/login$/);
  await expect(employeePage.getByText('Сеанс завершён. Войдите снова')).toBeVisible();

  await fillLogin(employeePage, employee.account.login, employee.account.password);
  await expect(employeePage.getByRole('alert')).toHaveText(
    'Учётная запись заблокирована. Обратитесь к администратору портала',
  );

  await openUserMenu(page, employee);
  await menuItem(page, 'Разблокировать').click();
  await expect(toast(page, 'Пользователь разблокирован')).toBeVisible();
  const api = await signIn(employee.account);
  expect((await (await api.get('/api/auth/session')).json()).step).toBe('ready');
  await api.dispose();
});

test('блокировка обрывает идущий ответ модели', async ({ adminApi, newMember }) => {
  const employee = await newMember();
  const dialogId = await createDialog(employee.api, 'chat');
  const stream = employee.api.post(`/api/dialogs/${dialogId}/messages`, {
    data: { content: 'Долгий ответ [[stub:slow]]', mode: 'fast', knowledge: 'none' },
  });
  // Блокируем, когда ответ уже формируется: у диалога появилось сообщение в состоянии «streaming».
  const observer = await signIn(employee.account);
  await expect
    .poll(async () => {
      const messages = await (await observer.get(`/api/dialogs/${dialogId}/messages`)).json();
      return messages.items.some((message: { status: string }) => message.status === 'streaming');
    })
    .toBe(true);

  expect((await adminApi.post(`/api/admin/users/${employee.account.id}/block`)).status()).toBe(200);

  const events = parseStream(await (await stream).text());
  expect(events.at(-1)).toMatchObject({ event: 'error', data: { code: 'session_ended' } });
  expect(eventsOf(events, 'done')).toHaveLength(0);
  await observer.dispose();
});

test('сброс пароля: прежний пароль не действует, вход — с временным и обязательной сменой', async ({
  newMember,
  pageAs,
}) => {
  const admin = await newMember('admin');
  const employee = await newMember();
  const page = await pageAs(admin, '/admin/users');

  await openUserMenu(page, employee);
  await menuItem(page, 'Сбросить пароль').click();
  await expect(page.getByText('Сбросить пароль?')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Сбросить' }).click();
  await expect(page.getByText('Пароль сброшен')).toBeVisible();
  const temporaryPassword = (await page.getByRole('dialog').locator('.p-mono').last().innerText()).trim();
  await page.getByRole('button', { name: 'Готово' }).click();

  await expectError(await employee.api.get('/api/auth/session'), 401, 'unauthenticated');

  const api = await newApi();
  await expectError(
    await api.post('/api/auth/login', { data: { login: employee.account.login, password: employee.account.password } }),
    401,
    'invalid_credentials',
  );
  // Сначала второй фактор, потом смена пароля: временный пароль в чужих руках ничего не даёт.
  const login = await api.post('/api/auth/login', {
    data: { login: employee.account.login, password: temporaryPassword },
  });
  expect((await login.json()).step).toBe('second_factor');
  const second = await api.post('/api/auth/second-factor', { data: { code: await nextCode(employee.account) } });
  expect((await second.json()).session.step).toBe('password_change');
  await expectError(await api.get('/api/dialogs?kind=chat'), 403, 'login_step_required');
  const changed = await api.post('/api/auth/password', { data: { new_password: `после-сброса-${uniqueSuffix()}` } });
  expect((await changed.json()).step).toBe('ready');
  await api.dispose();
});

test('сброс второго фактора: прежние коды не подходят, настройка проходит заново', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const employee = await newMember();
  const page = await pageAs(admin, '/admin/users');

  await openUserMenu(page, employee);
  await menuItem(page, 'Сбросить второй фактор').click();
  await expect(page.getByText('Сбросить второй фактор?')).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Сбросить' }).click();
  await expect(toast(page, 'Второй фактор сброшен')).toBeVisible();
  await expect(page.getByRole('row').filter({ hasText: employee.account.login })).toContainText('Не настроен');

  await expectError(await employee.api.get('/api/auth/session'), 401, 'unauthenticated');

  const api = await newApi();
  const login = await api.post('/api/auth/login', {
    data: { login: employee.account.login, password: employee.account.password },
  });
  expect((await login.json()).step).toBe('second_factor_setup');
  await expectError(await api.get('/api/dialogs?kind=chat'), 403, 'login_step_required');

  const setup = await (await api.post('/api/auth/second-factor/setup')).json();
  expect(setup.secret).not.toBe(employee.account.secret);
  // Код от прежнего ключа настройку не подтверждает.
  await expectError(
    await api.post('/api/auth/second-factor/confirm', { data: { code: usedCode(employee.account) } }),
    422,
    'invalid_code',
  );
  const fresh = { secret: setup.secret as string, lastStep: 0 };
  const confirm = await api.post('/api/auth/second-factor/confirm', { data: { code: await nextCode(fresh) } });
  expect((await confirm.json()).session.step).toBe('ready');
  await api.dispose();
});

test('сброс пароля администратором снимает блокировку логина', async ({ adminApi, newMember }) => {
  const employee = await newMember();
  const api = await newApi();
  for (let attempt = 0; attempt < 6; attempt += 1) {
    await api.post('/api/auth/login', { data: { login: employee.account.login, password: 'неверный-пароль-000' } });
  }
  const reset = await adminApi.post(`/api/admin/users/${employee.account.id}/reset-password`);
  expect(reset.status()).toBe(200);
  const login = await api.post('/api/auth/login', {
    data: { login: employee.account.login, password: (await reset.json()).temporary_password },
  });
  expect(login.status(), await login.text()).toBe(200);
  await api.dispose();
});

test('администратор меняет ФИО и роль; смена роли завершает сеансы пользователя', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const employee = await newMember();
  const page = await pageAs(admin, '/admin/users');
  const renamed = `Переименованный Сотрудник ${employee.account.login}`;

  await openUserMenu(page, employee);
  await menuItem(page, 'Изменить').click();
  await expect(page.getByText('Изменить учётную запись')).toBeVisible();
  await page.getByRole('textbox', { name: 'Фамилия, имя, отчество' }).fill(renamed);
  await page.getByRole('button', { name: 'Сохранить' }).click();
  await expect(toast(page, 'Учётная запись изменена')).toBeVisible();
  // Смена только ФИО сеанс не трогает.
  expect((await (await employee.api.get('/api/auth/session')).json()).user.full_name).toBe(renamed);

  await page.getByRole('button', { name: `Действия: ${renamed}` }).click();
  await menuItem(page, 'Изменить').click();
  await page.getByText('Администратор — управляет пользователями').click();
  await page.getByRole('button', { name: 'Сохранить' }).click();
  await expect(page.getByText('Сменить роль на „Администратор“?')).toBeVisible();
  await page.getByRole('button', { name: 'Сменить роль' }).click();
  await expect(toast(page, 'Роль изменена')).toBeVisible();
  await expectError(await employee.api.get('/api/auth/session'), 401, 'unauthenticated');
});

test('свою учётную запись администратор изменить не может', async ({ newMember }) => {
  const admin = await newMember('admin');
  const own = `/api/admin/users/${admin.account.id}`;
  await expectError(await admin.api.post(`${own}/block`), 409, 'cannot_modify_self');
  await expectError(await admin.api.post(`${own}/reset-password`), 409, 'cannot_modify_self');
  await expectError(await admin.api.post(`${own}/reset-second-factor`), 409, 'cannot_modify_self');
  await expectError(await admin.api.post(`${own}/unlock-login`), 409, 'cannot_modify_self');
  await expectError(await admin.api.patch(own, { data: { role: 'employee' } }), 409, 'cannot_modify_self');
});

test('сотруднику раздел «Пользователи» закрыт', async ({ newMember, pageAs }) => {
  const employee = await newMember();
  const page = await pageAs(employee, '/admin/users');
  await expect(page.getByRole('heading', { name: 'Нет доступа' })).toBeVisible();
  await expect(page.getByText('Этот раздел открыт только администраторам')).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Разделы портала' }).getByRole('link', { name: 'Пользователи' })).toHaveCount(0);
});

test('поиск по списку пользователей: пустой результат и сброс', async ({ newMember, pageAs }) => {
  const admin = await newMember('admin');
  const page = await pageAs(admin, '/admin/users');
  await page.getByRole('textbox', { name: 'Найти по ФИО или логину' }).fill(`нет-такого-${uniqueSuffix()}`);
  await expect(page.getByRole('heading', { name: 'Никого не нашлось' })).toBeVisible();
  await page.getByRole('button', { name: 'Сбросить поиск' }).click();
  await expect(page.getByRole('textbox', { name: 'Найти по ФИО или логину' })).toHaveValue('');
  await expect(page.getByRole('heading', { name: 'Никого не нашлось' })).toHaveCount(0);
  await expect(page.getByRole('row').filter({ hasText: 'Активна' }).first()).toBeVisible();
});

test.describe('Временная блокировка входа', () => {
  const LOCK_NOTE = /Вход временно заблокирован до \d{2}:\d{2}, после неудачных попыток/;

  test('администратор видит блокировку и снимает её: пароль, код и открытая сессия сотрудника прежние', async ({
    newMember,
    pageAs,
  }) => {
    const admin = await newMember('admin');
    const employee = await newMember();
    const page = await pageAs(admin, '/admin/users');
    const row = page.getByRole('row').filter({ hasText: employee.account.login });
    await searchUsers(page, employee.account.login);
    await expect(row).toHaveCount(1);
    await expect(row).not.toContainText('Вход временно заблокирован');

    await lockLogin(employee.account.login);
    await page.reload();
    await searchUsers(page, employee.account.login);
    await expect(row).toContainText(LOCK_NOTE);
    await expect(row).toContainText('Активна');

    await page.getByRole('button', { name: `Действия: ${employee.account.fullName}` }).click();
    const item = page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Снять блокировку входа' });
    await expect(item).toContainText('Пароль и код из приложения останутся прежними');
    await item.click();
    // Без подтверждения: сразу уведомление, пометка и пункт меню исчезают.
    await expect(
      toast(page, 'Блокировка входа снята: пользователь может войти с прежним паролем и кодом из приложения'),
    ).toBeVisible();
    await expect(page.getByRole('dialog')).toHaveCount(0);
    await expect(row).not.toContainText('Вход временно заблокирован');
    await page.getByRole('button', { name: `Действия: ${employee.account.fullName}` }).click();
    await expect(menuItem(page, 'Заблокировать')).toBeVisible();
    await expect(page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Снять блокировку входа' })).toHaveCount(0);

    // Сессия, открытая до блокировки, жива; вход — с прежним паролем и кодом, без смены и настройки.
    const session = await (await employee.api.get('/api/auth/session')).json();
    expect(session).toMatchObject({ step: 'ready', user: { second_factor_configured: true } });
    const api = await signIn(employee.account);
    expect((await (await api.get('/api/auth/session')).json()).step).toBe('ready');
    await api.dispose();

    // Повторное снятие — успех без изменений.
    const again = await admin.api.post(`/api/admin/users/${employee.account.id}/unlock-login`);
    expect(again.status()).toBe(200);
    expect(await again.json()).toMatchObject({ unlocked: false, user: { login_locked_until: null, state: 'active' } });
  });

  test('снятие через API: срок в данных пользователя, сотруднику маршрут закрыт', async ({ newMember }) => {
    const admin = await newMember('admin');
    const employee = await newMember();
    const other = await newMember();
    const target = `/api/admin/users/${employee.account.id}`;

    await lockLogin(employee.account.login);
    const list = await (await admin.api.get(`/api/admin/users?q=${employee.account.login}`)).json();
    const until = Date.parse(list.items[0].login_locked_until);
    expect(until).toBeGreaterThan(Date.now());

    await expectError(await other.api.post(`${target}/unlock-login`), 403, 'forbidden');
    await expectError(await employee.api.post(`${target}/unlock-login`), 403, 'forbidden');
    // Отказ ничего не снял: вход по-прежнему закрыт.
    const api = await newApi();
    await expectError(
      await api.post('/api/auth/login', { data: { login: employee.account.login, password: employee.account.password } }),
      429,
      'login_locked',
    );

    const unlocked = await admin.api.post(`${target}/unlock-login`);
    expect(unlocked.status()).toBe(200);
    expect(await unlocked.json()).toMatchObject({ unlocked: true, user: { login_locked_until: null } });
    const login = await api.post('/api/auth/login', {
      data: { login: employee.account.login, password: employee.account.password },
    });
    expect(login.status(), await login.text()).toBe(200);
    expect((await login.json()).step).toBe('second_factor');
    await api.dispose();
  });

  test('снятие блокировки входа не разблокирует заблокированную учётную запись', async ({ newMember }) => {
    const admin = await newMember('admin');
    const employee = await newMember();
    const target = `/api/admin/users/${employee.account.id}`;
    expect((await admin.api.post(`${target}/block`)).status()).toBe(200);
    await lockLogin(employee.account.login);

    const list = await (await admin.api.get(`/api/admin/users?q=${employee.account.login}`)).json();
    expect(list.items[0].state).toBe('blocked');
    expect(list.items[0].login_locked_until).not.toBeNull();

    const unlocked = await admin.api.post(`${target}/unlock-login`);
    expect(await unlocked.json()).toMatchObject({ unlocked: true, user: { state: 'blocked', login_locked_until: null } });
    const api = await newApi();
    await expectError(
      await api.post('/api/auth/login', { data: { login: employee.account.login, password: employee.account.password } }),
      403,
      'account_blocked',
    );
    await api.dispose();
  });

  test('в своей строке администратор видит пометку, но снять блокировку себе не может', async ({ newMember, pageAs }) => {
    const admin = await newMember('admin');
    await lockLogin(admin.account.login);

    const page = await pageAs(admin, '/admin/users');
    await searchUsers(page, admin.account.login);
    const row = page.getByRole('row').filter({ hasText: admin.account.login });
    await expect(row).toContainText(LOCK_NOTE);
    await expect(row.getByRole('button')).toHaveCount(0);
    await expectError(await admin.api.post(`/api/admin/users/${admin.account.id}/unlock-login`), 409, 'cannot_modify_self');
  });
});
