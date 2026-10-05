/** Общие заготовки тестов: администратор прогона, новые пользователи, страница под пользователем. */
import { readFileSync } from 'node:fs';

import { test as base, request, type APIRequestContext, type Page } from '@playwright/test';

import { adoptSession, provisionUser, type Account, type Role } from './accounts';
import { ADMIN_STATE_FILE, BASE_URL } from './stand';

export interface Member {
  account: Account;
  api: APIRequestContext;
}

interface Fixtures {
  /** Клиент API администратора прогона: только чтобы создавать пользователей теста. */
  adminApi: APIRequestContext;
  /** Создаёт нового пользователя с завершённым первым входом. */
  newMember: (role?: Role) => Promise<Member>;
  /** Открывает новую страницу браузера под пользователем. */
  pageAs: (member: Member, path?: string) => Promise<Page>;
}

export const test = base.extend<Fixtures>({
  // Playwright требует деструктуризацию первым параметром, даже когда заготовке ничего не нужно.
  // eslint-disable-next-line no-empty-pattern
  adminApi: async ({}, provide) => {
    const { storage } = JSON.parse(readFileSync(ADMIN_STATE_FILE, 'utf8'));
    const api = await request.newContext({
      baseURL: BASE_URL,
      ignoreHTTPSErrors: true,
      extraHTTPHeaders: { 'X-Portal-Csrf': '1' },
      storageState: storage,
    });
    await provide(api);
    await api.dispose();
  },

  newMember: async ({ adminApi }, provide) => {
    const created: APIRequestContext[] = [];
    await provide(async (role = 'employee') => {
      const member = await provisionUser(adminApi, role);
      created.push(member.api);
      return member;
    });
    await Promise.all(created.map((api) => api.dispose()));
  },

  pageAs: async ({ browser }, provide) => {
    const contexts: { close: () => Promise<void> }[] = [];
    await provide(async (member, path = '/chat') => {
      const context = await browser.newContext();
      contexts.push(context);
      await adoptSession(context, member.api);
      const page = await context.newPage();
      await page.goto(path);
      return page;
    });
    await Promise.all(contexts.map((context) => context.close()));
  },
});

export { expect } from '@playwright/test';
