/** Стенд `portal/dev/`: адрес и команды бэкенда в контейнере `portal-api`. */
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const BASE_URL = process.env.PORTAL_E2E_BASE_URL ?? 'https://localhost:8443';
export const COMPOSE_FILE = path.resolve(HERE, '../../../dev/docker-compose.yml');
/** Состояние между глобальной настройкой и тестами: учётная запись администратора прогона. */
export const STATE_DIR = path.resolve(HERE, '../.state');
export const ADMIN_STATE_FILE = path.join(STATE_DIR, 'admin.json');

/** Команда `portal …` в контейнере `portal-api` стенда; возвращает её вывод. */
export function portalCli(...args: string[]): string {
  return execFileSync('docker', ['compose', '-f', COMPOSE_FILE, 'exec', '-T', 'portal-api', 'portal', ...args], {
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'pipe'],
  });
}
