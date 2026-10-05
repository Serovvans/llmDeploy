/**
 * Критерий 12 (docs/portal-design.md §10) на стенде: тот же Caddyfile, что на ВМ. Без сессии
 * доступны только страница входа и статика; `/v1/*` требует ключ; админ-API Bifrost,
 * `/metrics` и служебные маршруты бэкенда недоступны. Заголовки безопасности §3.2 —
 * часть критерия 13; на ВМ они перепроверяются по чек-листу.
 */
import { newApi } from '../support/accounts';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';

test('без сессии: страница входа и статика открываются, остальное закрыто', async ({ page }) => {
  const api = await newApi();

  for (const path of ['/', '/login', '/chat', '/knowledge', '/admin/users']) {
    const response = await api.get(path);
    expect(response.status(), path).toBe(200);
    expect(response.headers()['content-type'], path).toContain('text/html');
  }
  // Любой рабочий адрес без сессии приводит на вход, а не показывает данные.
  for (const path of ['/chat', '/knowledge', '/sql', '/cogis', '/documents', '/admin/users', '/profile']) {
    await page.goto(path);
    await expect(page, path).toHaveURL(/\/login$/);
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
  }
  const html = await (await api.get('/login')).text();
  const assets = [...html.matchAll(/(?:src|href)="(\/[^"]+)"/g)].map((match) => match[1] ?? '');
  expect(assets.length).toBeGreaterThan(0);
  for (const asset of assets) {
    expect((await api.get(asset)).status(), asset).toBe(200);
  }

  for (const path of [
    '/api/governance/virtual-keys',
    '/api/governance/customers',
    '/metrics',
    '/docs',
    '/redoc',
    '/openapi.json',
    '/healthz',
    '/api/healthz',
    '/.env',
  ]) {
    expect((await api.get(path)).status(), path).toBe(404);
  }
  await api.dispose();
});

test('/v1/* без ключа не отвечает', async () => {
  const api = await newApi();
  expect((await api.get('/v1/models')).status()).toBe(401);
  const completion = await api.post('/v1/chat/completions', {
    data: { model: 'default', messages: [{ role: 'user', content: 'Привет' }] },
  });
  expect(completion.status()).toBe(401);
  // Сессия портала ключа не заменяет: cookie в /v1 не уходит (Path=/api).
  await api.dispose();
});

test('неизвестный маршрут и чужой метод /api — 404 в общем формате ошибки на русском', async ({ newMember }) => {
  const member = await newMember();
  for (const response of [await member.api.get('/api/nosuch'), await member.api.put('/api/dialogs')]) {
    expect(response.status()).toBe(404);
    const body = await response.json();
    expect(body.error.code).toBe('not_found');
    expect(body.error.message).toMatch(/[А-Яа-я]/);
    expect(body.error.message).not.toMatch(/[A-Za-z]/);
  }
});

test('заголовки безопасности на странице, API и статике; ответы API не кэшируются', async ({ newMember }) => {
  const member = await newMember();
  const html = await (await member.api.get('/login')).text();
  const script = must(/src="(\/assets\/[^"]+\.js)"/.exec(html)?.[1], 'адрес скрипта на странице входа');

  for (const path of ['/login', '/api/auth/session', script]) {
    const headers = (await member.api.get(path)).headers();
    expect(headers['strict-transport-security'], path).toContain('max-age=');
    expect(headers['content-security-policy'], path).toBe(
      "default-src 'self'; style-src 'self' 'unsafe-inline'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
    );
    expect(headers['x-frame-options'], path).toBe('DENY');
    expect(headers['x-content-type-options'], path).toBe('nosniff');
    expect(headers['referrer-policy'], path).toBe('no-referrer');
  }
  expect((await member.api.get('/api/auth/session')).headers()['cache-control']).toBe('no-store');
  expect((await member.api.get('/api/config')).headers()['cache-control']).toBe('no-store');
});

test('страница не обращается к чужим адресам и не нарушает политику содержимого', async ({ newMember, pageAs }) => {
  const member = await newMember('admin');
  const page = await pageAs(member, '/chat');
  const origin = new URL(page.url()).origin;
  const foreign: string[] = [];
  const violations: string[] = [];
  page.on('request', (request) => {
    const url = request.url();
    if (!url.startsWith(origin) && !url.startsWith('blob:') && !url.startsWith('data:')) {
      foreign.push(url);
    }
  });
  page.on('console', (message) => {
    if (/Content Security Policy|Content-Security-Policy/i.test(message.text())) {
      violations.push(message.text());
    }
  });

  for (const path of ['/chat', '/knowledge', '/sql', '/cogis', '/documents', '/admin/users', '/profile']) {
    await page.goto(path);
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
    // Шрифты загружены: их запросы тоже попали в список.
    await page.evaluate(() => document.fonts.ready.then(() => undefined));
  }
  expect(foreign).toEqual([]);
  expect(violations).toEqual([]);
  // Шрифты с кириллицей раздаёт сам портал.
  expect(await page.evaluate(() => document.fonts.check('16px "Golos Text"', 'Портал'))).toBe(true);
});
