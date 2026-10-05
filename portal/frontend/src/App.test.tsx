import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { ADMIN, EMPLOYEE, fail, mockApi, ok, session } from './test/mockApi';
import { renderApp } from './test/renderApp';
import { texts } from './texts';

const SETUP = { secret: 'JBSWY3DPEHPK3PXP', qr: { size: 37, path: 'M0 0h7v7h-7z' } };
const NEW_USER = { ...EMPLOYEE, second_factor_configured: false, backup_codes: null };

function path(): string {
  return screen.getByTestId('path').textContent ?? '';
}

async function fillLogin(user: ReturnType<typeof userEvent.setup>, password = 'временный-пароль') {
  await user.type(await screen.findByLabelText(texts.login.loginLabel), 'ivanov');
  await user.type(screen.getByLabelText(texts.login.passwordLabel), password);
  await user.click(screen.getByRole('button', { name: texts.login.submit }));
}

describe('шаги входа', () => {
  it('корневой адрес без сессии ведёт на вход без сообщений', async () => {
    mockApi({ 'GET /api/auth/session': () => fail(401, 'unauthenticated') });
    renderApp('/');

    expect(await screen.findByRole('heading', { level: 1, name: texts.product })).toBeInTheDocument();
    expect(path()).toBe('/login');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(screen.getByLabelText(texts.login.loginLabel)).toHaveFocus();
  });

  it('корневой адрес с завершённым входом ведёт в чат', async () => {
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/');

    expect(await screen.findByRole('heading', { level: 1, name: texts.chat.newChat })).toBeInTheDocument();
    expect(path()).toBe('/chat');
    await waitFor(() => expect(document.title).toBe('Новый чат — Портал сотрудников'));
  });

  it('первый вход: вход → смена пароля → настройка второго фактора → резервные коды → чат', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => fail(401, 'unauthenticated'),
      'POST /api/auth/login': () => ok(session('password_change', NEW_USER)),
      'POST /api/auth/password': () => ok(session('second_factor_setup', NEW_USER)),
      'POST /api/auth/second-factor/setup': () => ok(SETUP),
      'POST /api/auth/second-factor/confirm': () =>
        ok({ session: session('ready'), backup_codes: ['4F7K-92QD', '8HMR-3TXA'] }),
    });
    renderApp('/');

    await fillLogin(user);
    expect(await screen.findByRole('heading', { level: 1, name: texts.password.title })).toBeInTheDocument();
    expect(path()).toBe('/password');
    expect(screen.getByText(texts.password.introFirst)).toBeInTheDocument();
    expect(await screen.findByText(texts.password.hint(12))).toBeInTheDocument();

    await user.type(screen.getByLabelText(texts.password.newLabel), 'новый пароль из слов');
    await user.type(screen.getByLabelText(texts.password.repeatLabel), 'новый пароль из слов');
    await user.click(screen.getByRole('button', { name: texts.password.submit }));

    expect(await screen.findByRole('heading', { level: 1, name: texts.secondFactor.title })).toBeInTheDocument();
    expect(path()).toBe('/second-factor');
    const qr = await screen.findByRole('img', { name: texts.secondFactor.qrLabel });
    expect(qr).toHaveAttribute('viewBox', '0 0 37 37');
    expect(qr.querySelector('path')).toHaveAttribute('d', SETUP.qr.path);

    await user.click(screen.getByRole('button', { name: texts.secondFactor.cannotScan }));
    expect(screen.getByText('JBSW Y3DP EHPK 3PXP')).toBeInTheDocument();

    await user.type(screen.getByLabelText(texts.secondFactor.step3Title), '123456');
    expect(await screen.findByRole('heading', { level: 1, name: texts.backupCodes.title })).toBeInTheDocument();
    expect(server.callsTo('POST /api/auth/second-factor/confirm')).toHaveLength(1);
    expect(screen.getByText('4F7K-92QD')).toBeInTheDocument();
    expect(path()).toBe('/second-factor');

    await user.click(screen.getByRole('button', { name: texts.backupCodes.proceed }));
    expect(screen.getByText(texts.backupCodes.saveFirst)).toBeInTheDocument();
    expect(path()).toBe('/second-factor');

    await user.click(screen.getByLabelText(texts.backupCodes.saved));
    await user.click(screen.getByRole('button', { name: texts.backupCodes.proceed }));
    expect(await screen.findByRole('heading', { level: 1, name: texts.chat.newChat })).toBeInTheDocument();
    expect(path()).toBe('/chat');
  });

  it('обычный вход: код отправляется сам после шестой цифры, открывается исходный адрес', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => fail(401, 'unauthenticated'),
      'POST /api/auth/login': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => ok({ session: session('ready'), backup_code_used: false }),
    });
    renderApp('/profile');

    await fillLogin(user, 'пароль');
    expect(await screen.findByRole('heading', { level: 1, name: texts.code.title })).toBeInTheDocument();
    expect(path()).toBe('/login/code');
    // До второго фактора сервер не отдаёт данных учётной записи — на экране их нет.
    expect(screen.queryByText(EMPLOYEE.full_name)).not.toBeInTheDocument();

    const code = screen.getByLabelText(texts.code.title);
    expect(code).toHaveFocus();
    await user.type(code, '123456');

    expect(await screen.findByRole('heading', { level: 1, name: texts.profile.title })).toBeInTheDocument();
    expect(path()).toBe('/profile');
    expect(server.callsTo('POST /api/auth/second-factor').map((call) => call.body)).toEqual([{ code: '123456' }]);
  });

  it('после сброса пароля: код → смена пароля с пояснением про администратора → чат', async () => {
    const user = userEvent.setup();
    mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => ok({ session: session('password_change'), backup_code_used: false }),
      'POST /api/auth/password': () => ok(session('ready')),
    });
    renderApp('/');

    await user.type(await screen.findByLabelText(texts.code.title), '123456');
    expect(await screen.findByText(texts.password.introReset)).toBeInTheDocument();

    await user.type(screen.getByLabelText(texts.password.newLabel), 'новый пароль из слов');
    await user.type(screen.getByLabelText(texts.password.repeatLabel), 'новый пароль из слов');
    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    await waitFor(() => expect(path()).toBe('/chat'));
  });

  it.each([
    ['second_factor', '/password', '/login/code'],
    ['password_change', '/login', '/password'],
    ['second_factor_setup', '/chat', '/second-factor'],
    ['ready', '/login/code', '/chat'],
  ] as const)('на шаге %s адрес %s ведёт на %s', async (step, from, to) => {
    mockApi({
      'GET /api/auth/session': () => ok(session(step)),
      'POST /api/auth/second-factor/setup': () => ok(SETUP),
    });
    renderApp(from);
    await waitFor(() => expect(path()).toBe(to));
  });

  it('отказ `login_step_required` перечитывает сессию и открывает экран её шага', async () => {
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready', ADMIN)),
      'GET /api/admin/users': () => fail(403, 'login_step_required', { details: { step: 'second_factor' } }),
    });
    renderApp('/chat');
    await screen.findByRole('heading', { level: 1, name: texts.chat.newChat });

    server.on('GET /api/auth/session', () => ok(session('second_factor')));
    await userEvent.setup().click(screen.getByRole('link', { name: texts.nav.users }));
    await waitFor(() => expect(path()).toBe('/login/code'));
  });

  it('ввод резервного кода: свои заголовок и поле, после входа — сообщение с остатком', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => ok({ session: session('ready'), backup_code_used: true }),
    });
    renderApp('/login/code');

    await user.click(await screen.findByRole('button', { name: texts.code.useBackupCode }));
    expect(screen.getByRole('heading', { level: 1, name: texts.code.backupTitle })).toBeInTheDocument();
    expect(screen.getByText(texts.code.backupIntro)).toBeInTheDocument();

    await user.type(screen.getByLabelText(texts.code.backupTitle), '4f7k 92qd');
    await user.click(screen.getByRole('button', { name: texts.code.submit }));

    expect(await screen.findByText('Резервный код использован. Осталось кодов: 7')).toBeInTheDocument();
    // Поле ничего не переформатирует: код уходит как введён.
    expect(server.callsTo('POST /api/auth/second-factor')[0]?.body).toEqual({ backup_code: '4f7k 92qd' });
    await waitFor(() => expect(path()).toBe('/chat'));
  });

  it('«Войти под другим логином» завершает сессию шага и возвращает на вход без сообщений', async () => {
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/logout': () => ok(),
    });
    renderApp('/login/code');

    await userEvent.setup().click(await screen.findByRole('button', { name: texts.code.otherLogin }));
    await waitFor(() => expect(path()).toBe('/login'));
    expect(server.callsTo('POST /api/auth/logout')).toHaveLength(1);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });
});

describe('отказы входа', () => {
  it('пустые поля: ошибки под полями, запрос не уходит', async () => {
    const server = mockApi({ 'GET /api/auth/session': () => fail(401, 'unauthenticated') });
    renderApp('/login');

    await userEvent.setup().click(await screen.findByRole('button', { name: texts.login.submit }));
    expect(screen.getByText(texts.login.enterLogin)).toBeInTheDocument();
    expect(screen.getByText(texts.login.enterPassword)).toBeInTheDocument();
    expect(screen.getByLabelText(texts.login.loginLabel)).toHaveFocus();
    expect(screen.getByLabelText(texts.login.loginLabel)).toHaveAccessibleDescription(texts.login.enterLogin);
    expect(server.callsTo('POST /api/auth/login')).toHaveLength(0);
  });

  it('неверный логин или пароль: заметка, поле пароля очищено и в фокусе', async () => {
    const user = userEvent.setup();
    mockApi({
      'GET /api/auth/session': () => fail(401, 'unauthenticated'),
      'POST /api/auth/login': () => fail(401, 'invalid_credentials'),
    });
    renderApp('/login');

    await fillLogin(user);
    expect(await screen.findByRole('alert')).toHaveTextContent(texts.login.invalidCredentials);
    const password = screen.getByLabelText(texts.login.passwordLabel);
    expect(password).toHaveValue('');
    await waitFor(() => expect(password).toHaveFocus());
    expect(screen.getByLabelText(texts.login.loginLabel)).toHaveValue('ivanov');
  });

  it.each([
    ['login_locked', 429, { retry_after_seconds: 290 }, 'Слишком много неудачных попыток. Попробуйте снова через 5 минут или попросите администратора портала снять блокировку'],
    ['too_many_attempts', 429, { retry_after_seconds: 30 }, 'Слишком много попыток входа. Попробуйте снова через минуту'],
    ['account_blocked', 403, undefined, texts.login.accountBlocked],
    ['service_unavailable', 503, undefined, texts.common.serverFailure],
  ])('отказ %s показывает свой текст', async (code, status, details, text) => {
    const user = userEvent.setup();
    mockApi({
      'GET /api/auth/session': () => fail(401, 'unauthenticated'),
      'POST /api/auth/login': () => fail(status, code, { details }),
    });
    renderApp('/login');

    await fillLogin(user);
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.getByRole('button', { name: texts.login.submit })).toBeEnabled();
  });

  it('нет связи с сервером при входе', async () => {
    const user = userEvent.setup();
    const server = mockApi({ 'GET /api/auth/session': () => fail(401, 'unauthenticated') });
    renderApp('/login');
    await screen.findByLabelText(texts.login.loginLabel);

    server.on('POST /api/auth/login', () => {
      throw new TypeError('Failed to fetch');
    });
    await fillLogin(user);
    expect(await screen.findByRole('alert')).toHaveTextContent(texts.login.noConnection);
  });

  it.each([
    ['invalid_code', texts.code.invalidCode],
    ['code_already_used', texts.code.codeAlreadyUsed],
  ])('код: отказ %s — текст под полем, поле в фокусе', async (code, text) => {
    mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => fail(422, code),
    });
    renderApp('/login/code');

    const field = await screen.findByLabelText(texts.code.title);
    await userEvent.setup().type(field, '123456');
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(field).toHaveAccessibleDescription(text);
    await waitFor(() => expect(field).toHaveFocus());
    expect(path()).toBe('/login/code');
  });

  it('неполный код на сервер не уходит: «Введите код» и «В коде шесть цифр»', async () => {
    const user = userEvent.setup();
    const server = mockApi({ 'GET /api/auth/session': () => ok(session('second_factor')) });
    renderApp('/login/code');

    const field = await screen.findByLabelText(texts.code.title);
    await user.click(screen.getByRole('button', { name: texts.code.submit }));
    expect(screen.getByRole('alert')).toHaveTextContent('Введите код');

    await user.type(field, '12345{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('В коде шесть цифр');
    await user.clear(field);
    await user.type(field, '12a45{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent('В коде шесть цифр');
    expect(server.callsTo('POST /api/auth/second-factor')).toHaveLength(0);
  });

  it('код уходит сам один раз на значение: после ошибки поле выделено, то же значение — только кнопкой', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => fail(422, 'invalid_code'),
    });
    renderApp('/login/code');
    const sent = () => server.callsTo('POST /api/auth/second-factor').map((call) => call.body);

    const field = (await screen.findByLabelText(texts.code.title)) as HTMLInputElement;
    await user.type(field, '123456');
    await screen.findByRole('alert');
    await waitFor(() => expect([field.selectionStart, field.selectionEnd]).toEqual([0, 6]));

    // Новый код набирается поверх выделенного и тоже уходит сам.
    await user.keyboard('654321');
    await waitFor(() => expect(sent()).toEqual([{ code: '123456' }, { code: '654321' }]));
    await waitFor(() => expect(field).toHaveFocus());

    // Прежнее значение, набранное снова, само не отправляется.
    await user.clear(field);
    await user.type(field, '123456');
    expect(sent()).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: texts.code.submit }));
    await waitFor(() => expect(sent()).toHaveLength(3));
  });

  it.each(['вставка', 'ввод'])('код с пробелом, как его показывает приложение (%s), даёт шесть цифр и уходит', async (way) => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => fail(422, 'invalid_code'),
    });
    renderApp('/login/code');

    const field = await screen.findByLabelText(texts.code.title);
    if (way === 'вставка') {
      await user.click(field);
      await user.paste('123 456');
    } else {
      await user.type(field, '123 456');
    }

    expect(field).toHaveValue('123456');
    await waitFor(() => expect(server.callsTo('POST /api/auth/second-factor').map((call) => call.body)).toEqual([{ code: '123456' }]));
    expect(await screen.findByRole('alert')).toHaveTextContent(texts.code.invalidCode);
  });

  it('резервный код сам не отправляется никогда; пустое поле — «Введите резервный код»', async () => {
    const user = userEvent.setup();
    const server = mockApi({ 'GET /api/auth/session': () => ok(session('second_factor')) });
    renderApp('/login/code');

    await user.click(await screen.findByRole('button', { name: texts.code.useBackupCode }));
    await user.click(screen.getByRole('button', { name: texts.code.submit }));
    expect(screen.getByRole('alert')).toHaveTextContent('Введите резервный код');

    await user.type(screen.getByLabelText(texts.code.backupTitle), '123456');
    expect(server.callsTo('POST /api/auth/second-factor')).toHaveLength(0);
  });

  it('неверный резервный код — текст под полем', async () => {
    const user = userEvent.setup();
    mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => fail(422, 'invalid_backup_code'),
    });
    renderApp('/login/code');

    await user.click(await screen.findByRole('button', { name: texts.code.useBackupCode }));
    await user.type(screen.getByLabelText(texts.code.backupTitle), 'XXXX-XXXX{Enter}');
    expect(await screen.findByRole('alert')).toHaveTextContent(texts.code.invalidBackupCode);
  });

  it.each([
    ['login_locked', 429, { retry_after_seconds: 600 }, 'Слишком много неудачных попыток. Попробуйте снова через 10 минут или попросите администратора портала снять блокировку'],
    ['login_step_expired', 401, undefined, texts.login.codeStepExpired],
  ])('код: отказ %s возвращает на вход с заметкой', async (code, status, details, text) => {
    mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => fail(status, code, { details }),
    });
    renderApp('/login/code');

    await userEvent.setup().type(await screen.findByLabelText(texts.code.title), '123456');
    await waitFor(() => expect(path()).toBe('/login'));
    expect(screen.getByRole('alert')).toHaveTextContent(text);
  });

  it('код: слишком частые попытки с адреса — заметка на этом же экране', async () => {
    mockApi({
      'GET /api/auth/session': () => ok(session('second_factor')),
      'POST /api/auth/second-factor': () => fail(429, 'too_many_attempts', { details: { retry_after_seconds: 120 } }),
    });
    renderApp('/login/code');

    await userEvent.setup().type(await screen.findByLabelText(texts.code.title), '123456');
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Слишком много попыток входа. Попробуйте снова через 2 минуты',
    );
    expect(path()).toBe('/login/code');
  });

  it('смена пароля: повтор не совпал — проверяет интерфейс; коды полей сервера — свои тексты', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('password_change', NEW_USER)),
      'POST /api/auth/password': () =>
        fail(422, 'validation_error', {
          fields: [{ field: 'new_password', code: 'password_too_short', message: 'Пароль короче 12 символов.' }],
        }),
    });
    renderApp('/password');

    const next = await screen.findByLabelText(texts.password.newLabel);
    const repeat = screen.getByLabelText(texts.password.repeatLabel);
    await user.type(next, 'один');
    await user.type(repeat, 'другой');
    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    expect(screen.getByRole('alert')).toHaveTextContent(texts.password.mismatch);
    expect(server.callsTo('POST /api/auth/password')).toHaveLength(0);

    await user.clear(repeat);
    await user.type(repeat, 'один');
    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    expect(await screen.findByText('Пароль короче 12 символов')).toBeInTheDocument();

    for (const [code, text] of [
      ['password_too_common', texts.password.tooCommon],
      ['password_same_as_old', texts.password.sameAsOld],
    ] as const) {
      server.on('POST /api/auth/password', () =>
        fail(422, 'validation_error', { fields: [{ field: 'new_password', code, message: 'Текст сервера.' }] }),
      );
      await user.click(screen.getByRole('button', { name: texts.password.submit }));
      expect(await screen.findByText(text)).toBeInTheDocument();
    }
  });

  it('смена пароля: пустые поля и предел длины проверяет интерфейс; `too_long` сервера — тот же текст', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('password_change', NEW_USER)),
      'POST /api/auth/password': () =>
        fail(422, 'validation_error', { fields: [{ field: 'new_password', code: 'too_long', message: 'Текст сервера.' }] }),
    });
    renderApp('/password');
    const next = await screen.findByLabelText(texts.password.newLabel);
    const repeat = screen.getByLabelText(texts.password.repeatLabel);
    await screen.findByText(texts.password.hint(12));

    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    expect(screen.getByText('Введите новый пароль')).toBeInTheDocument();
    expect(screen.getByText('Повторите пароль', { selector: '[role="alert"]' })).toBeInTheDocument();

    const long = 'я'.repeat(129);
    await user.click(next);
    await user.paste(long);
    await user.click(repeat);
    await user.paste(long);
    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    expect(screen.getByText('Пароль длиннее 128 символов')).toBeInTheDocument();
    expect(server.callsTo('POST /api/auth/password')).toHaveLength(0);

    await user.clear(next);
    await user.type(next, 'обычный пароль из слов');
    await user.clear(repeat);
    await user.type(repeat, 'обычный пароль из слов');
    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    expect(await screen.findByText('Пароль длиннее 128 символов')).toBeInTheDocument();
    expect(server.callsTo('POST /api/auth/password')).toHaveLength(1);
  });

  it('срок незавершённого входа вышел на смене пароля — вход с заметкой про тот же шаг', async () => {
    const user = userEvent.setup();
    mockApi({
      'GET /api/auth/session': () => ok(session('password_change', NEW_USER)),
      'POST /api/auth/password': () => fail(401, 'login_step_expired'),
    });
    renderApp('/password');

    await user.type(await screen.findByLabelText(texts.password.newLabel), 'новый пароль из слов');
    await user.type(screen.getByLabelText(texts.password.repeatLabel), 'новый пароль из слов');
    await user.click(screen.getByRole('button', { name: texts.password.submit }));
    await waitFor(() => expect(path()).toBe('/login'));
    expect(screen.getByRole('alert')).toHaveTextContent(texts.login.loginStepExpired);
  });

  it('настройка второго фактора: сбой запроса — заметка с «Повторить»; `setup_not_started` — тихий перезапрос', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('second_factor_setup', NEW_USER)),
      'POST /api/auth/second-factor/setup': () => fail(500, 'internal_error'),
      'POST /api/auth/second-factor/confirm': () => fail(409, 'setup_not_started'),
    });
    renderApp('/second-factor');

    expect(await screen.findByText(texts.secondFactor.qrFailed)).toBeInTheDocument();
    server.on('POST /api/auth/second-factor/setup', () => ok(SETUP));
    await user.click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByRole('img', { name: texts.secondFactor.qrLabel })).toBeInTheDocument();

    const before = server.callsTo('POST /api/auth/second-factor/setup').length;
    const field = screen.getByLabelText(texts.secondFactor.step3Title);
    await user.type(field, '123456');
    await waitFor(() => expect(server.callsTo('POST /api/auth/second-factor/setup')).toHaveLength(before + 1));
    await waitFor(() => expect(field).toHaveValue(''));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();

    server.on('POST /api/auth/second-factor/confirm', () => fail(422, 'invalid_code'));
    await user.type(field, '654321');
    expect(await screen.findByRole('alert')).toHaveTextContent(texts.secondFactor.invalidCode);
  });
});

describe('конец сессии и служебные экраны', () => {
  it('сессия закончилась во время работы — вход с заметкой «Сеанс завершён», после входа тот же адрес', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready', ADMIN)),
      'GET /api/admin/users': () => fail(401, 'unauthenticated'),
    });
    renderApp('/chat');

    await user.click(await screen.findByRole('link', { name: texts.nav.users }));
    await waitFor(() => expect(path()).toBe('/login'));
    expect(screen.getByRole('status')).toHaveTextContent(texts.common.sessionEnded);

    server.on('POST /api/auth/login', () => ok(session('ready', ADMIN)));
    server.on('GET /api/admin/users', () => ok({ items: [], page: 1, page_size: 50, total: 0 }));
    await fillLogin(user);
    await waitFor(() => expect(path()).toBe('/admin/users'));
  });

  it('собственный выход — вход без сообщений, после входа открывается чат', async () => {
    const user = userEvent.setup();
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'POST /api/auth/logout': () => ok(),
      'POST /api/auth/login': () => ok(session('ready')),
    });
    renderApp('/profile');

    await user.click(await screen.findByRole('button', { name: texts.profile.logout }));
    await waitFor(() => expect(path()).toBe('/login'));
    expect(server.callsTo('POST /api/auth/logout')).toHaveLength(1);
    expect(screen.queryByRole('status')).not.toBeInTheDocument();

    await fillLogin(user);
    await waitFor(() => expect(path()).toBe('/chat'));
  });

  it('портал не отвечает при открытии — служебный экран и повтор', async () => {
    const server = mockApi({ 'GET /api/auth/session': () => fail(503, 'service_unavailable') });
    renderApp('/chat');

    expect(await screen.findByRole('heading', { level: 1, name: texts.service.unavailable.title })).toBeInTheDocument();
    expect(screen.getByText(texts.service.unavailable.text)).toBeInTheDocument();

    server.on('GET /api/auth/session', () => ok(session('ready')));
    await userEvent.setup().click(screen.getByRole('button', { name: texts.service.unavailable.action }));
    expect(await screen.findByRole('heading', { level: 1, name: texts.chat.newChat })).toBeInTheDocument();
  });

  it('неизвестный адрес — «Страница не найдена» с переходом в чат', async () => {
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/metrics');

    expect(await screen.findByRole('heading', { level: 1, name: texts.service.notFound.title })).toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: texts.common.goToChat }));
    expect(path()).toBe('/chat');
  });

  it('рейка: пять разделов; «Пользователи» — только у администратора', async () => {
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    const view = renderApp('/chat');

    const rail = await screen.findByRole('navigation', { name: texts.nav.label });
    expect(Array.from(rail.querySelectorAll('a')).map((link) => link.textContent)).toEqual([
      'Чат',
      'База знаний',
      'SQL',
      'CoGIS',
      'Документы',
    ]);
    expect(screen.getByRole('link', { name: texts.nav.chat })).toHaveAttribute('aria-current', 'page');
    view.unmount();

    mockApi({ 'GET /api/auth/session': () => ok(session('ready', ADMIN)) });
    renderApp('/chat');
    expect(await screen.findByRole('link', { name: texts.nav.users })).toBeInTheDocument();
  });

  it('сотрудник по прямому адресу раздела пользователей видит «Нет доступа»', async () => {
    const server = mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/admin/users');

    expect(await screen.findByRole('heading', { level: 1, name: texts.users.noAccess.title })).toBeInTheDocument();
    expect(screen.getByText(texts.users.noAccess.text)).toBeInTheDocument();
    expect(server.callsTo('GET /api/admin/users')).toHaveLength(0);
  });
});
