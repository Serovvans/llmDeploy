/**
 * Критерий 2 (docs/portal-design.md §10): администратор создаёт и блокирует пользователя,
 * сбрасывает пароль и второй фактор; заблокированный теряет доступ сразу.
 */
import type { Page } from '@playwright/test';

import { newApi, nextCode, signIn, uniqueSuffix, usedCode } from '../support/accounts';
import { expect, test, type Member } from '../support/fixtures';
import { createDialog, eventsOf, expectError, parseStream } from '../support/portal';
import { fillLogin, toast } from '../support/ui';

/** Открывает меню действий в строке пользователя, найдя его поиском по логину. */
async function openUserMenu(page: Page, member: Member): Promise<void> {
  await page.getByRole('textbox', { name: 'Найти по ФИО или логину' }).fill(member.account.login);
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

  await page.getByRole('textbox', { name: 'Найти по ФИО или логину' }).fill(login);
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
