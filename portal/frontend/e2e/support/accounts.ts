/**
 * Учётные записи для тестов: каждая создаётся заново и проходит первый вход через API
 * (смена пароля, настройка второго фактора), как это сделал бы сотрудник.
 */
import { randomBytes } from 'node:crypto';

import { expect, request, type APIRequestContext, type BrowserContext } from '@playwright/test';

import { BASE_URL } from './stand';
import { currentStep, totpAt } from './totp';

export type Role = 'admin' | 'employee';

export interface Account {
  id: string;
  login: string;
  fullName: string;
  role: Role;
  password: string;
  /** Ключ второго фактора в base32. */
  secret: string;
  backupCodes: string[];
  /** Последний шаг времени, код которого уже предъявлен: сервер не принимает шаг повторно. */
  lastStep: number;
}

export interface CreatedUser {
  id: string;
  login: string;
  fullName: string;
  role: Role;
  temporaryPassword: string;
}

/** Сколько шагов вперёд от текущего принимает сервер (`auth.totp.window_steps`). */
const WINDOW_STEPS = 1;

export function uniqueSuffix(): string {
  return randomBytes(5).toString('hex');
}

export function newPassword(): string {
  return `e2e-${randomBytes(9).toString('hex')}`;
}

/** Клиент API со своим набором cookie и заголовком защиты от CSRF. */
export function newApi(options: { csrf?: boolean } = {}): Promise<APIRequestContext> {
  return request.newContext({
    baseURL: BASE_URL,
    ignoreHTTPSErrors: true,
    extraHTTPHeaders: options.csrf === false ? {} : { 'X-Portal-Csrf': '1' },
  });
}

/**
 * Следующий код из «приложения»: шаг строго больше уже использованного. Сервер принимает
 * соседний шаг, поэтому второй вход подряд проходит сразу; третий ждёт смены шага времени.
 */
export async function nextCode(account: Pick<Account, 'secret' | 'lastStep'>): Promise<string> {
  const step = Math.max(currentStep(), account.lastStep + 1);
  await expect.poll(() => currentStep() + WINDOW_STEPS, { timeout: 65_000 }).toBeGreaterThanOrEqual(step);
  account.lastStep = step;
  return totpAt(account.secret, step);
}

/** Код шага, который уже использован: для проверок повторного кода. */
export function usedCode(account: Pick<Account, 'secret' | 'lastStep'>): string {
  return totpAt(account.secret, account.lastStep);
}

/** Создаёт пользователя от имени администратора. */
export async function createUser(admin: APIRequestContext, role: Role = 'employee'): Promise<CreatedUser> {
  const suffix = uniqueSuffix();
  const fullName = `Сквозной Тест ${suffix}`;
  const response = await admin.post('/api/admin/users', {
    data: { full_name: fullName, login: `e2e-${suffix}`, role },
  });
  expect(response.status(), await response.text()).toBe(201);
  const body = await response.json();
  return { id: body.user.id, login: body.user.login, fullName, role, temporaryPassword: body.temporary_password };
}

/** Первый вход: временный пароль → новый пароль → настройка второго фактора. */
export async function completeFirstLogin(
  user: Omit<CreatedUser, 'id'> & { id?: string },
): Promise<{ account: Account; api: APIRequestContext }> {
  const api = await newApi();
  const password = newPassword();

  const login = await api.post('/api/auth/login', { data: { login: user.login, password: user.temporaryPassword } });
  expect(login.status(), await login.text()).toBe(200);
  expect((await login.json()).step).toBe('password_change');

  const changed = await api.post('/api/auth/password', { data: { new_password: password } });
  expect(changed.status(), await changed.text()).toBe(200);
  expect((await changed.json()).step).toBe('second_factor_setup');

  const setup = await api.post('/api/auth/second-factor/setup');
  expect(setup.status(), await setup.text()).toBe(200);
  const secret: string = (await setup.json()).secret;

  const pending = { secret, lastStep: 0 };
  const confirm = await api.post('/api/auth/second-factor/confirm', { data: { code: await nextCode(pending) } });
  expect(confirm.status(), await confirm.text()).toBe(200);
  const confirmed = await confirm.json();
  expect(confirmed.session.step).toBe('ready');

  return {
    api,
    account: {
      id: user.id ?? confirmed.session.user.id,
      login: user.login,
      fullName: user.fullName,
      role: user.role,
      password,
      secret,
      backupCodes: confirmed.backup_codes,
      lastStep: pending.lastStep,
    },
  };
}

/** Новый пользователь с завершённым первым входом и открытой сессией. */
export async function provisionUser(
  admin: APIRequestContext,
  role: Role = 'employee',
): Promise<{ account: Account; api: APIRequestContext }> {
  return completeFirstLogin(await createUser(admin, role));
}

/** Обычный вход через API: пароль и код. */
export async function signIn(account: Account): Promise<APIRequestContext> {
  const api = await newApi();
  const login = await api.post('/api/auth/login', { data: { login: account.login, password: account.password } });
  expect(login.status(), await login.text()).toBe(200);
  const second = await api.post('/api/auth/second-factor', { data: { code: await nextCode(account) } });
  expect(second.status(), await second.text()).toBe(200);
  return api;
}

/** Переносит сессию клиента API в браузер: страница открывается уже под этим пользователем. */
export async function adoptSession(context: BrowserContext, api: APIRequestContext): Promise<void> {
  await context.addCookies((await api.storageState()).cookies);
}

/** Наибольший срок блокировки входа на стенде (`auth.lockout.max_seconds` в portal/dev/config.override.yaml). */
const STAND_MAX_LOCK_SECONDS = 8;

/**
 * Закрывает вход для логина серией неверных паролей и доводит блокировку до наибольшего на
 * стенде срока: первая блокировка длится пару секунд, каждая неудача после её конца удлиняет
 * следующую. Так у сценария остаётся запас времени, пока блокировка действует.
 */
export async function lockLogin(login: string): Promise<void> {
  const api = await newApi();
  await expect
    .poll(
      async () => {
        const response = await api.post('/api/auth/login', { data: { login, password: 'неверный-пароль-000' } });
        return response.status() === 429 ? (await response.json()).error.details.retry_after_seconds : 0;
      },
      { timeout: 45_000, intervals: [100] },
    )
    .toBeGreaterThanOrEqual(STAND_MAX_LOCK_SECONDS - 1);
  await api.dispose();
}
