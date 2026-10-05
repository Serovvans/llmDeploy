/**
 * Перед прогоном: администратор прогона создаётся командой бэкенда на стенде (как первый
 * администратор на ВМ, docs/portal-design.md §4) и проходит первый вход. Тесты от его имени
 * только создают своих пользователей; учётная запись у каждого прогона новая.
 */
import { mkdirSync, writeFileSync } from 'node:fs';

import { completeFirstLogin, uniqueSuffix } from './support/accounts';
import { ADMIN_STATE_FILE, STATE_DIR, portalCli } from './support/stand';

export default async function globalSetup(): Promise<void> {
  const login = `e2e-admin-${uniqueSuffix()}`;
  const fullName = 'Администратор Сквозных Тестов';
  const output = portalCli('create-admin', '--login', login, '--full-name', fullName);
  const temporaryPassword = /Временный пароль:\s*(\S+)/.exec(output)?.[1];
  if (!temporaryPassword) {
    throw new Error('Команда create-admin не вывела временный пароль');
  }

  const { account, api } = await completeFirstLogin({ login, fullName, role: 'admin', temporaryPassword });
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(ADMIN_STATE_FILE, JSON.stringify({ account, storage: await api.storageState() }), { mode: 0o600 });
  await api.dispose();
}
