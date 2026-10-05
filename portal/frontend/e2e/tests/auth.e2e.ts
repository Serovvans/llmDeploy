/**
 * Критерий 1 (docs/portal-design.md §10): первый вход, смена пароля, второй фактор,
 * отказ без кода, с неверным и повторным кодом, резервный код. Критерий 2 — блокировка
 * логина после серии неудач.
 */
import { createUser, newApi, nextCode, usedCode } from '../support/accounts';
import { expect, test } from '../support/fixtures';
import { expectError } from '../support/portal';
import { currentStep, totpAt } from '../support/totp';
import { fillLogin, signInThroughUi, signOutThroughUi, toast } from '../support/ui';

/** Число неудач подряд до блокировки логина (`auth.lockout.max_failures`). */
const MAX_FAILURES = 5;

test.describe('Первый вход', () => {
  test('временный пароль → новый пароль → второй фактор → чат; следующий вход требует пароль и код', async ({
    adminApi,
    page,
  }) => {
    const user = await createUser(adminApi);
    const password = `новый-пароль-${user.login}`;

    await page.goto('/');
    await expect(page).toHaveURL(/\/login$/);
    await fillLogin(page, user.login, user.temporaryPassword);

    await expect(page.getByRole('heading', { name: 'Придумайте новый пароль' })).toBeVisible();
    await page.getByRole('textbox', { name: /^Новый пароль/ }).fill(password);
    await page.getByRole('textbox', { name: /^Повторите пароль/ }).fill(password);
    await page.getByRole('button', { name: 'Сохранить пароль' }).click();

    await expect(page.getByRole('heading', { name: 'Защитите вход кодом из телефона' })).toBeVisible();
    await expect(page.getByRole('img', { name: 'QR-код для приложения-аутентификатора' })).toBeVisible();
    await page.getByRole('button', { name: 'Не получается отсканировать' }).click();
    const secret = (await page.locator('.p-mono').first().innerText()).replace(/\s/g, '');
    expect(secret).toMatch(/^[A-Z2-7]{32}$/);

    const setupStep = currentStep();
    await page.getByRole('textbox', { name: 'Введите код из приложения' }).fill(totpAt(secret, setupStep));

    await expect(page.getByRole('heading', { name: 'Сохраните резервные коды' })).toBeVisible();
    await expect(page.getByText(/^[0-9A-Z]{4}-[0-9A-Z]{4}$/)).toHaveCount(10);
    // Без отметки «сохранил» дальше не пускает.
    await page.getByRole('button', { name: 'Перейти к работе' }).click();
    await expect(page.getByRole('alert')).toHaveText('Сначала сохраните коды — потом их нельзя будет посмотреть');
    await page.getByText('Я сохранил(а) коды').click();
    await page.getByRole('button', { name: 'Перейти к работе' }).click();

    await expect(page).toHaveURL(/\/chat$/);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

    await signOutThroughUi(page);
    // Временный пароль больше не действует.
    await fillLogin(page, user.login, user.temporaryPassword);
    await expect(page.getByRole('alert')).toHaveText('Неверный логин или пароль');

    await fillLogin(page, user.login, password);
    await expect(page.getByRole('heading', { name: 'Код из приложения' })).toBeVisible();
    const account = { secret, lastStep: setupStep };
    await page.getByRole('textbox', { name: 'Код из приложения' }).fill(await nextCode(account));
    await expect(page).toHaveURL(/\/chat$/);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
  });

  test('слабый пароль не принимается: короткий, распространённый, с опечаткой в повторе', async ({ adminApi, page }) => {
    const user = await createUser(adminApi);
    await page.goto('/login');
    await fillLogin(page, user.login, user.temporaryPassword);
    await expect(page.getByRole('heading', { name: 'Придумайте новый пароль' })).toBeVisible();

    const newPassword = page.getByRole('textbox', { name: /^Новый пароль/ });
    const repeat = page.getByRole('textbox', { name: /^Повторите пароль/ });
    const save = page.getByRole('button', { name: 'Сохранить пароль' });

    await newPassword.fill('короткий');
    await repeat.fill('короткий');
    await save.click();
    await expect(page.getByRole('alert')).toHaveText('Пароль короче 12 символов');

    await newPassword.fill('password1234');
    await repeat.fill('password1234');
    await save.click();
    await expect(page.getByRole('alert')).toHaveText('Такой пароль слишком легко подобрать. Придумайте другой');

    await newPassword.fill('достаточно-длинный-пароль');
    await repeat.fill('достаточно-длинный-пороль');
    await save.click();
    await expect(page.getByRole('alert')).toHaveText('Пароли не совпадают');
    await expect(page).toHaveURL(/\/password$/);
  });

  test('неверный код при настройке второго фактора не включает его', async ({ adminApi, page }) => {
    const user = await createUser(adminApi);
    await page.goto('/login');
    await fillLogin(page, user.login, user.temporaryPassword);
    const password = `новый-пароль-${user.login}`;
    await page.getByRole('textbox', { name: /^Новый пароль/ }).fill(password);
    await page.getByRole('textbox', { name: /^Повторите пароль/ }).fill(password);
    await page.getByRole('button', { name: 'Сохранить пароль' }).click();
    await page.getByRole('button', { name: 'Не получается отсканировать' }).click();
    const secret = (await page.locator('.p-mono').first().innerText()).replace(/\s/g, '');

    // Код далёкого шага времени заведомо не подходит.
    await page.getByRole('textbox', { name: 'Введите код из приложения' }).fill(totpAt(secret, currentStep() + 1000));
    await expect(page.getByRole('alert')).toContainText('Код не подошёл');
    await expect(page).toHaveURL(/\/second-factor$/);

    // Пока второй фактор не настроен, рабочие экраны закрыты.
    await page.goto('/chat');
    await expect(page).toHaveURL(/\/second-factor$/);
  });
});

test.describe('Второй фактор при входе', () => {
  test('без кода доступа нет: ни к экранам, ни к API', async ({ newMember, page }) => {
    const { account } = await newMember();
    await page.goto('/login');
    await fillLogin(page, account.login, account.password);
    await expect(page.getByRole('heading', { name: 'Код из приложения' })).toBeVisible();

    for (const path of ['/chat', '/knowledge', '/profile']) {
      await page.goto(path);
      await expect(page).toHaveURL(/\/login\/code$/);
    }
    await expectError(await page.request.get('/api/dialogs?kind=chat'), 403, 'login_step_required');
    await expectError(await page.request.get('/api/kb/documents?scope=shared'), 403, 'login_step_required');
    // До второго фактора API не отдаёт данных учётной записи.
    expect((await (await page.request.get('/api/auth/session')).json()).user).toBeNull();
  });

  test('неверный код не пускает, верный после него — пускает', async ({ newMember, page }) => {
    const { account } = await newMember();
    await page.goto('/login');
    await fillLogin(page, account.login, account.password);

    const code = page.getByRole('textbox', { name: 'Код из приложения' });
    await code.fill(totpAt(account.secret, currentStep() + 1000));
    await expect(page.getByRole('alert').first()).toHaveText(
      'Код не подошёл. Дождитесь нового кода в приложении и введите его',
    );
    await expect(page).toHaveURL(/\/login\/code$/);

    await code.fill(await nextCode(account));
    await expect(page).toHaveURL(/\/chat$/);
  });

  test('повторно использованный код не принимается', async ({ newMember, page }) => {
    const { account } = await newMember();
    await page.goto('/login');
    await fillLogin(page, account.login, account.password);

    // Этим кодом пользователь уже подтвердил настройку второго фактора.
    const code = page.getByRole('textbox', { name: 'Код из приложения' });
    await code.fill(usedCode(account));
    await expect(page.getByRole('alert').first()).toHaveText(
      'Этот код уже использован. Дождитесь, когда приложение покажет следующий',
    );
    await expect(page).toHaveURL(/\/login\/code$/);

    // Код, которым только что вошли, для второго входа уже не годится.
    await code.fill(await nextCode(account));
    await expect(page).toHaveURL(/\/chat$/);
    const second = await newApi();
    await second.post('/api/auth/login', { data: { login: account.login, password: account.password } });
    await expectError(
      await second.post('/api/auth/second-factor', { data: { code: usedCode(account) } }),
      422,
      'code_already_used',
    );
    await second.dispose();
  });

  test('резервный код срабатывает один раз', async ({ newMember, page }) => {
    const { account } = await newMember();
    const [backupCode = '', spareCode = ''] = account.backupCodes;

    await page.goto('/login');
    await fillLogin(page, account.login, account.password);
    await page.getByRole('button', { name: 'Ввести резервный код' }).click();
    await expect(page.getByRole('heading', { name: 'Резервный код' })).toBeVisible();
    await page.getByRole('textbox', { name: 'Резервный код' }).fill(backupCode);
    await page.getByRole('button', { name: 'Подтвердить' }).click();

    await expect(page).toHaveURL(/\/chat$/);
    await expect(toast(page, 'Резервный код использован. Осталось кодов: 9')).toBeVisible();
    await page.goto('/profile');
    await expect(page.getByText('Осталось 9 из 10')).toBeVisible();

    await signOutThroughUi(page);
    await fillLogin(page, account.login, account.password);
    await page.getByRole('button', { name: 'Ввести резервный код' }).click();
    await page.getByRole('textbox', { name: 'Резервный код' }).fill(backupCode);
    await page.getByRole('button', { name: 'Подтвердить' }).click();
    await expect(page.getByRole('alert').first()).toHaveText(
      'Резервный код не подошёл. Проверьте, что он не был использован раньше',
    );
    await expect(page).toHaveURL(/\/login\/code$/);

    // Другой резервный код по-прежнему годится.
    await page.getByRole('textbox', { name: 'Резервный код' }).fill(spareCode);
    await page.getByRole('button', { name: 'Подтвердить' }).click();
    await expect(page).toHaveURL(/\/chat$/);
  });
});

test.describe('Ограничение перебора', () => {
  test('серия неверных паролей временно блокирует логин, затем вход снова возможен', async ({ newMember, page }) => {
    const { account } = await newMember();
    const api = await newApi();
    for (let attempt = 0; attempt < MAX_FAILURES; attempt += 1) {
      await expectError(
        await api.post('/api/auth/login', { data: { login: account.login, password: 'неверный-пароль-000' } }),
        401,
        'invalid_credentials',
      );
    }

    // Пока блокировка действует, не пускает и верный пароль.
    const locked = await api.post('/api/auth/login', { data: { login: account.login, password: account.password } });
    await expectError(locked, 429, 'login_locked');
    expect(Number(locked.headers()['retry-after'])).toBeGreaterThan(0);
    expect((await locked.json()).error.details.retry_after_seconds).toBeGreaterThan(0);

    await page.goto('/login');
    await fillLogin(page, account.login, account.password);
    await expect(page.getByRole('alert')).toHaveText('Слишком много неудачных попыток. Попробуйте снова через минуту');

    // Блокировка временная: на стенде она длится секунды (portal/dev/config.override.yaml).
    await expect
      .poll(
        async () =>
          (await api.post('/api/auth/login', { data: { login: account.login, password: account.password } })).status(),
        { timeout: 30_000 },
      )
      .toBe(200);
    await expect(
      (await api.post('/api/auth/second-factor', { data: { code: await nextCode(account) } })).status(),
    ).toBe(200);
    await api.dispose();
  });

  test('серия неверных кодов блокирует логин и гасит незавершённый вход', async ({ newMember }) => {
    const { account } = await newMember();
    const api = await newApi();
    await api.post('/api/auth/login', { data: { login: account.login, password: account.password } });
    const wrongCode = totpAt(account.secret, currentStep() + 1000);
    for (let attempt = 0; attempt < MAX_FAILURES - 1; attempt += 1) {
      await expectError(await api.post('/api/auth/second-factor', { data: { code: wrongCode } }), 422, 'invalid_code');
    }
    await expectError(await api.post('/api/auth/second-factor', { data: { code: wrongCode } }), 429, 'login_locked');
    await expectError(await api.get('/api/auth/session'), 401, 'unauthenticated');
    await expectError(
      await api.post('/api/auth/login', { data: { login: account.login, password: account.password } }),
      429,
      'login_locked',
    );
    await api.dispose();
  });

  test('неизвестный логин и неверный пароль отвечают одинаково', async ({ newMember }) => {
    const { account } = await newMember();
    const api = await newApi();
    const unknown = await api.post('/api/auth/login', {
      data: { login: `нет-такого-${account.login}`, password: 'неверный-пароль-000' },
    });
    const wrong = await api.post('/api/auth/login', { data: { login: account.login, password: 'неверный-пароль-000' } });
    expect(unknown.status()).toBe(401);
    expect(wrong.status()).toBe(401);
    expect(await unknown.json()).toEqual(await wrong.json());
    await api.dispose();
  });
});

test('выход завершает сеанс: рабочие экраны и API закрыты', async ({ newMember, page }) => {
  const { account } = await newMember();
  await signInThroughUi(page, account);
  await signOutThroughUi(page);
  await expectError(await page.request.get('/api/auth/session'), 401, 'unauthenticated');
  await page.goto('/knowledge');
  await expect(page).toHaveURL(/\/login$/);
});
