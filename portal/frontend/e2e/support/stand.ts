/** Стенд `portal/dev/`: адрес и команды бэкенда в контейнере `portal-api`. */
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const BASE_URL = process.env.PORTAL_E2E_BASE_URL ?? 'https://localhost:8443';
export const COMPOSE_FILE = path.resolve(HERE, '../../../dev/docker-compose.yml');
/** Состояние между глобальной настройкой и тестами: учётная запись администратора прогона. */
export const STATE_DIR = path.resolve(HERE, '../.state');
export const ADMIN_STATE_FILE = path.join(STATE_DIR, 'admin.json');

/**
 * Имя проекта compose стенда — из строки `name:` его файла, как в scripts/dev_stand.sh.
 * Оно передаётся явно: с `COMPOSE_PROJECT_NAME` в окружении команда иначе ушла бы в чужой
 * проект — на машине с рабочим стеком в его `portal-api`.
 */
function composeProject(): string {
  const name = /^name:\s*["']?([^"'\s]+)/m.exec(readFileSync(COMPOSE_FILE, 'utf8'))?.[1];
  if (!name) {
    throw new Error(`В ${COMPOSE_FILE} нет строки name: — проект стенда не определить`);
  }
  return name;
}

/** Команда `portal …` в контейнере `portal-api` стенда; возвращает её вывод. */
export function portalCli(...args: string[]): string {
  return execFileSync(
    'docker',
    ['compose', '-p', composeProject(), '-f', COMPOSE_FILE, 'exec', '-T', 'portal-api', 'portal', ...args],
    { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
  );
}
