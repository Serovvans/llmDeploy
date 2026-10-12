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

/** Команда `docker compose …` в проекте стенда; возвращает её вывод байтами. */
export function standCompose(...args: string[]): Buffer {
  return execFileSync('docker', ['compose', '-p', composeProject(), '-f', COMPOSE_FILE, ...args], {
    stdio: ['ignore', 'pipe', 'pipe'],
    maxBuffer: 64 * 1024 * 1024,
  });
}

/** Команда `portal …` в контейнере `portal-api` стенда; возвращает её вывод. */
export function portalCli(...args: string[]): string {
  return standCompose('exec', '-T', 'portal-api', 'portal', ...args).toString('utf8');
}

/** Команда базы знаний `portal …` в контейнере `portal-worker` (docs/portal-api.md §13.4). */
export function workerCli(...args: string[]): string {
  return standCompose('exec', '-T', 'portal-worker', 'portal', ...args).toString('utf8');
}

/**
 * Короткое видео со звуком (Matroska, H.264 + AAC): его собирает ffmpeg из образа воркера стенда,
 * чтобы тесты не зависели от программ на машине разработчика. `seed` уходит в метаданные — у каждого
 * теста свои байты.
 */
export function standVideo(seconds: number, seed: string): Buffer {
  return standCompose(
    'exec', '-T', 'portal-worker', 'ffmpeg', '-hide_banner', '-loglevel', 'error',
    '-f', 'lavfi', '-i', `testsrc=d=${seconds}:s=160x120:r=5`,
    '-f', 'lavfi', '-i', `sine=d=${seconds}`,
    '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', '-shortest',
    '-metadata', `title=${seed}`, '-f', 'matroska', 'pipe:1',
  );
}

/** Размеры файлов записей сотрудника в томе `portal-files` (docs/portal-api.md §1.6), в байтах. */
export function standMediaFiles(ownerId: string): number[] {
  const listing = standCompose(
    'exec', '-T', 'portal-worker', 'sh', '-c',
    `find /data/files/media/${ownerId} -type f -exec stat -c %s {} + 2>/dev/null || true`,
  ).toString('utf8');
  return listing.split('\n').filter(Boolean).map(Number);
}
