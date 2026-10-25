/** Стенд `portal/dev/`: адрес и команды бэкенда в контейнере `portal-api`. */
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const BASE_URL = process.env.PORTAL_E2E_BASE_URL ?? 'https://localhost:8443';
export const COMPOSE_FILE = path.resolve(HERE, '../../../dev/docker-compose.yml');
/** Накладка конфигурации стенда, которую compose монтирует в portal-api и portal-worker. */
const STAND_OVERRIDE_FILE = path.resolve(HERE, '../../../dev/config.override.yaml');
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

/** Запрос к базе портала стенда; строки ответа без заголовков, поля через `|`. */
export function standSql(query: string): string[] {
  return standCompose('exec', '-T', 'portal-db', 'psql', '-U', 'portal', '-d', 'portal', '-At', '-c', query)
    .toString('utf8')
    .split('\n')
    .filter(Boolean);
}

/** Журнал `portal-worker` с момента `since` (ISO 8601). */
export function workerLog(since: string): string {
  return standCompose('logs', '--no-color', '--no-log-prefix', '--since', since, 'portal-worker').toString('utf8');
}

/**
 * Перезапускает `portal-worker` с накладкой конфигурации стенда, дополненной `extraYaml` — разделами
 * верхнего уровня, которых в накладке нет (например, `kb:`). Возвращает функцию, которая
 * перезапускает воркер с прежней накладкой. Файлы стенда не меняются: дополненная накладка и
 * файл compose с её монтированием пишутся в каталог состояния прогона.
 */
export function restartWorkerWith(extraYaml: string): () => void {
  const base = readFileSync(STAND_OVERRIDE_FILE, 'utf8');
  for (const section of extraYaml.match(/^[A-Za-z_]+:/gm) ?? []) {
    if (new RegExp(`^${section}`, 'm').test(base)) {
      throw new Error(`Раздел ${section} уже есть в ${STAND_OVERRIDE_FILE}: дополнение его бы не слило, а заменило`);
    }
  }
  mkdirSync(STATE_DIR, { recursive: true });
  const override = path.join(STATE_DIR, 'worker-override.yaml');
  const composeFile = path.join(STATE_DIR, 'worker-override.compose.yml');
  writeFileSync(override, `${base}\n${extraYaml}`);
  // Тома службы compose сливает по пути в контейнере: эта строка заменяет монтирование накладки.
  writeFileSync(
    composeFile,
    `services:\n  portal-worker:\n    volumes:\n      - ${JSON.stringify(`${override}:/etc/portal/config.override.yaml:ro`)}\n`,
  );
  const up = ['up', '-d', '--no-build', '--no-deps', '--force-recreate', '--wait', 'portal-worker'];
  standCompose('-f', composeFile, ...up);
  return () => {
    standCompose(...up);
  };
}

/**
 * Перезапускает `portal-api` с накладкой стенда, в которой раздел `code:` заменён на `codeYaml`
 * (флаги и пределы раздела «Код», docs/portal-api.md §7.5.15). Возвращает функцию, которая
 * перезапускает `portal-api` с прежней накладкой. Файлы стенда не меняются: как у `restartWorkerWith`.
 */
export function restartApiWith(codeYaml: string): () => void {
  if (!/^code:\n/.test(codeYaml)) {
    throw new Error('Замена накладки portal-api начинается с раздела code:');
  }
  const base = readFileSync(STAND_OVERRIDE_FILE, 'utf8');
  // Раздел верхнего уровня — от строки `code:` до следующей строки без отступа (или до конца файла).
  const withoutCode = base.replace(/^code:\n(?:(?:[ #].*)?\n)*/m, '');
  mkdirSync(STATE_DIR, { recursive: true });
  const override = path.join(STATE_DIR, 'api-override.yaml');
  const composeFile = path.join(STATE_DIR, 'api-override.compose.yml');
  writeFileSync(override, `${withoutCode}\n${codeYaml}`);
  writeFileSync(
    composeFile,
    `services:\n  portal-api:\n    volumes:\n      - ${JSON.stringify(`${override}:/etc/portal/config.override.yaml:ro`)}\n`,
  );
  const up = ['up', '-d', '--no-build', '--no-deps', '--force-recreate', '--wait', 'portal-api'];
  standCompose('-f', composeFile, ...up);
  return () => {
    standCompose(...up);
  };
}

/** Сколько запросов `chat/completions` получила заглушка модели с момента `since` (ISO 8601). */
export function stubCompletions(since: string): number {
  return standCompose('logs', '--no-color', '--no-log-prefix', '--since', since, 'bifrost')
    .toString('utf8')
    .split('\n')
    .filter((line) => line.includes('POST /v1/chat/completions')).length;
}

/** Строка журнала `prompt usage` portal-api (docs/portal-api.md §5.10.10). */
export interface PromptUsage {
  answer_id: string;
  estimated_tokens: number;
  /** Расход по заглушке: символы всех текстовых частей запроса, который она получила, на 3, и 1500 на картинку. */
  prompt_tokens: number;
  text_chars: number;
  images: number;
}

/** Строка `prompt usage` ответа `answerId` в журнале portal-api с момента `since` (ISO 8601); нет — `null`. */
export function promptUsage(answerId: string, since: string): PromptUsage | null {
  const lines = standCompose('logs', '--no-color', '--no-log-prefix', '--since', since, 'portal-api')
    .toString('utf8')
    .split('\n');
  for (const line of lines) {
    if (line.includes('"prompt usage"') && line.includes(answerId)) {
      return JSON.parse(line) as PromptUsage;
    }
  }
  return null;
}
