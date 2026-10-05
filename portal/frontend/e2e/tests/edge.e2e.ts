/**
 * Критерий 12 (docs/portal-design.md §10) на стенде: тот же Caddyfile, что на ВМ. Без сессии
 * доступны только страница входа и статика; `/v1/*` требует ключ; админ-API Bifrost,
 * `/metrics` и служебные маршруты бэкенда недоступны. Заголовки безопасности §3.2 —
 * часть критерия 13; на ВМ они перепроверяются по чек-листу.
 */
import type { IncomingHttpHeaders } from 'node:http';
import { request as httpsRequest } from 'node:https';

import { newApi } from '../support/accounts';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { BASE_URL } from '../support/stand';

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

test('/v1: открытые маршруты модели требуют ключ, остальное под /v1 закрыто самим Caddy', async () => {
  const api = await newApi();
  // Четыре маршрута модели проксируются; без ключа — отказ. Сессия портала ключа не заменяет:
  // cookie в /v1 не уходит (Path=/api).
  const models = await api.get('/v1/models');
  expect([401, 403]).toContain(models.status());
  const completion = await api.post('/v1/chat/completions', {
    data: { model: 'default', messages: [{ role: 'user', content: 'Привет' }] },
  });
  expect([401, 403]).toContain(completion.status());
  expect(models.headers()['alt-svc']).toBeUndefined();

  // Всё прочее под /v1 до шлюза не доходит: 404 с пустым телом, а не страница портала.
  const closed = [
    await api.get('/v1/unknown-route'),
    await api.get('/v1/mcp/tools'),
    await api.get('/v1/skills'),
    await api.get('/v1'),
    await api.get('/v1/'),
    await api.post('/v1/async/chat/completions', { data: { model: 'default', messages: [] } }),
    await api.post('/v1/mcp/tool/execute', { data: {} }),
  ];
  for (const response of closed) {
    expect(response.status(), response.url()).toBe(404);
    expect((await response.body()).length, response.url()).toBe(0);
    expect(response.headers()['alt-svc'], response.url()).toBeUndefined();
  }
  await api.dispose();
});

/** Запрос с путём как есть: клиент Playwright нормализовал бы `..` и не дал бы проверить обход. */
function rawRequest(path: string): Promise<{ status: number; body: string; headers: IncomingHttpHeaders }> {
  const { hostname, port } = new URL(BASE_URL);
  return new Promise((resolve, reject) => {
    const request = httpsRequest(
      { hostname, port, path, method: 'POST', rejectUnauthorized: false, headers: { 'content-length': 0 } },
      (response) => {
        let body = '';
        response.on('data', (chunk) => (body += chunk));
        response.on('end', () => resolve({ status: response.statusCode ?? 0, body, headers: response.headers }));
      },
    );
    request.on('error', reject);
    request.end();
  });
}

test('/v1: варианты записи пути не открывают закрытые маршруты шлюза', async () => {
  for (const path of [
    '/V1/async/chat/completions',
    '/v1//async/chat/completions',
    '/v1/chat/completions/../async/chat/completions',
    '/v1/%61sync/chat/completions',
    '/v1/chat/completions/%2e%2e/async/chat/completions',
  ]) {
    const response = await rawRequest(path);
    expect(response.status, path).toBe(404);
    expect(response.body, path).toBe('');
    expect(response.headers['alt-svc'], path).toBeUndefined();
  }
  // Тем же способом открытый маршрут отвечает отказом по ключу: проверка не ложная.
  expect([401, 403]).toContain((await rawRequest('/v1/chat/completions')).status);
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

test('значок сайта отдаётся в обоих форматах и не нарушает политику содержимого', async ({ page }) => {
  const violations: string[] = [];
  page.on('console', (message) => {
    if (/Content.Security.Policy/i.test(message.text())) {
      violations.push(message.text());
    }
  });

  const svg = await page.request.get('/favicon.svg');
  expect(svg.status()).toBe(200);
  expect(svg.headers()['content-type']).toContain('image/svg+xml');
  expect(await svg.text()).toContain('<svg');
  const ico = await page.request.get('/favicon.ico');
  expect(ico.status()).toBe(200);
  expect(ico.headers()['content-type']).toMatch(/image\/(x-icon|vnd\.microsoft\.icon)/);
  // Сигнатура файла ICO: одно изображение.
  expect((await ico.body()).subarray(0, 6).equals(Buffer.from([0, 0, 1, 0, 1, 0]))).toBe(true);

  await page.goto('/login');
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
  await expect(page.locator('link[rel="icon"][href="/favicon.svg"]')).toHaveAttribute('type', 'image/svg+xml');
  await expect(page.locator('link[rel="icon"][href="/favicon.ico"]')).toHaveCount(1);
  // SVG со встроенным <style> открыт как документ: политика содержимого его не ломает.
  await page.goto('/favicon.svg');
  expect(violations).toEqual([]);
});
