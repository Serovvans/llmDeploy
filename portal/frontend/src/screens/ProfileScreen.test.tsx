import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { EMPLOYEE, fail, mockApi, ok, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

const t = texts.profile;

describe('профиль', () => {
  it.each(['light', 'dark'] as const)('показывает учётную запись и состояние входа в теме %s', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/profile');

    expect(await screen.findByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: t.accountTitle })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: t.loginTitle })).toBeInTheDocument();
    expect(screen.getByText(EMPLOYEE.full_name)).toBeInTheDocument();
    expect(screen.getByText('ivanov')).toBeInTheDocument();
    expect(screen.getByText('Сотрудник')).toBeInTheDocument();
    expect(screen.getByText('Осталось 7 из 10')).toBeInTheDocument();
    expect(screen.queryByText(t.backupCodesLow)).not.toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(theme);
  });

  it('при остатке резервных кодов 2 и меньше — предупреждение', async () => {
    mockApi({
      'GET /api/auth/session': () => ok(session('ready', { ...EMPLOYEE, backup_codes: { remaining: 2, total: 10 } })),
    });
    renderApp('/profile');

    expect(await screen.findByText('Осталось 2 из 10')).toBeInTheDocument();
    expect(screen.getByText(t.backupCodesLow)).toBeInTheDocument();
  });

  it('окно «Сменить пароль» не закрывается нажатием на фон', async () => {
    const user = userEvent.setup();
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/profile');

    await user.click(await screen.findByRole('button', { name: t.changePassword }));
    const dialog = within(await screen.findByRole('dialog'));
    await user.type(dialog.getByLabelText(texts.password.currentLabel), 'старый пароль');

    const background = document.querySelector('[data-tid="modal-container"]') as HTMLElement;
    fireEvent.mouseDown(background);
    fireEvent.mouseUp(background);
    fireEvent.click(background);
    expect(dialog.getByLabelText(texts.password.currentLabel)).toHaveValue('старый пароль');
  });

  it('на экране профиля пункт «Профиль» в рейке отмечен текущим', async () => {
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/profile');
    await screen.findByRole('heading', { level: 1, name: t.title });
    expect(within(screen.getByRole('navigation')).getByRole('button', { name: /Профиль/ })).toHaveAttribute('aria-current', 'true');
  });

  it('смена пароля: ошибки полей, после сохранения — вход с заметкой «Пароль изменён»', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'POST /api/auth/password': () =>
        fail(422, 'validation_error', {
          fields: [{ field: 'current_password', code: 'current_password_invalid', message: 'Текст сервера.' }],
        }),
    });
    renderApp('/profile');

    await user.click(await screen.findByRole('button', { name: t.changePassword }));
    const dialog = within(await screen.findByRole('dialog'));

    await user.click(dialog.getByRole('button', { name: texts.password.submit }));
    expect(dialog.getByText(texts.password.enterCurrent)).toBeInTheDocument();
    expect(dialog.getByText(texts.password.enterNew)).toBeInTheDocument();
    expect(dialog.getByText(texts.password.enterRepeat, { selector: '[role="alert"]' })).toBeInTheDocument();

    await user.type(dialog.getByLabelText(texts.password.currentLabel), 'старый пароль');
    await user.type(dialog.getByLabelText(texts.password.newLabel), 'новый пароль из слов');
    await user.type(dialog.getByLabelText(texts.password.repeatLabel), 'новый пароль из слоф');
    await user.click(dialog.getByRole('button', { name: texts.password.submit }));
    expect(dialog.getByText(texts.password.mismatch)).toBeInTheDocument();
    expect(server.callsTo('POST /api/auth/password')).toHaveLength(0);

    await user.clear(dialog.getByLabelText(texts.password.repeatLabel));
    await user.type(dialog.getByLabelText(texts.password.repeatLabel), 'новый пароль из слов');
    await user.click(dialog.getByRole('button', { name: texts.password.submit }));
    expect(await dialog.findByText(texts.password.currentInvalid)).toBeInTheDocument();

    // Серия неверных текущих паролей блокирует логин: заметка в окне, окно открыто, поля не очищены.
    server.on('POST /api/auth/password', () => fail(429, 'login_locked', { details: { retry_after_seconds: 300 } }));
    await user.click(dialog.getByRole('button', { name: texts.password.submit }));
    expect(await dialog.findByText('Слишком много неудачных попыток. Попробуйте снова через 5 минут')).toBeInTheDocument();
    expect(dialog.getByLabelText(texts.password.currentLabel)).toHaveValue('старый пароль');
    expect(dialog.getByRole('button', { name: texts.password.submit })).toBeEnabled();
    expect(screen.getByTestId('path')).toHaveTextContent('/profile');

    server.on('POST /api/auth/password', () => ok());
    await user.click(dialog.getByRole('button', { name: texts.password.submit }));

    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/login'));
    expect(screen.getByRole('status')).toHaveTextContent(texts.login.passwordChanged);
    expect(server.callsTo('POST /api/auth/password').at(-1)?.body).toEqual({
      new_password: 'новый пароль из слов',
      current_password: 'старый пароль',
    });
  });
});
