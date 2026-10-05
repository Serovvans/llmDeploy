/**
 * Сквозные тесты портала на стенде без GPU (`portal/dev/`, docs/portal-design.md §9, §10).
 *
 * Запуск (стенд уже поднят: `docker compose -f portal/dev/docker-compose.yml up -d --wait`):
 *   npm ci && npm run browsers     один раз
 *   npm test                       все браузеры
 *   npm test -- --project=chromium один браузер
 *
 * Файлы тестов называются `*.e2e.ts`, а не `*.spec.ts`: иначе их подобрал бы vitest фронтенда.
 * HTML-отчёт не включён: его каталог содержит JS, на котором споткнулся бы `eslint .` фронтенда.
 */
import { defineConfig, devices } from '@playwright/test';

import { BASE_URL } from './support/stand';

/** Тесты с этой меткой проверяют поведение самого браузера (просмотр PDF, скачивание DOCX). */
const BROWSER_SPECIFIC = /@браузер/;

export default defineConfig({
  testDir: './tests',
  testMatch: '**/*.e2e.ts',
  outputDir: './test-results',
  globalSetup: './global-setup.ts',
  fullyParallel: true,
  forbidOnly: true,
  retries: 0,
  workers: 4,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['list']],
  use: {
    baseURL: BASE_URL,
    // Сертификат стенда выпускает собственный CA Caddy.
    ignoreHTTPSErrors: true,
    locale: 'ru-RU',
    timezoneId: 'Europe/Moscow',
    colorScheme: 'light',
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'], viewport: { width: 1440, height: 900 } } },
    {
      name: 'firefox',
      use: {
        ...devices['Desktop Firefox'],
        viewport: { width: 1440, height: 900 },
        // Сборка Firefox для Playwright выключает встроенный просмотр PDF; обычный Firefox его показывает.
        launchOptions: { firefoxUserPrefs: { 'pdfjs.disabled': false } },
      },
    },
    { name: 'webkit', use: { ...devices['Desktop Safari'], viewport: { width: 1440, height: 900 } } },
    // Установленный в системе Chrome: ближайшая замена Edge (тот же движок), с настоящим просмотром PDF.
    {
      name: 'chrome',
      grep: BROWSER_SPECIFIC,
      use: { ...devices['Desktop Chrome'], channel: 'chrome', viewport: { width: 1440, height: 900 } },
    },
  ],
});
