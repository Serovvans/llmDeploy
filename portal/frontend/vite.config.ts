import { readFileSync } from 'node:fs';

import react from '@vitejs/plugin-react';
import { loadEnv, type Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

/** Порог размера файла сборки, кБ: файл библиотеки компонентов — около 1003 кБ и делению не поддаётся. */
const CHUNK_LIMIT_KB = 1050;
/**
 * Monaco с переводом и языками JavaScript и CSS (1.0) — около 3,7 МБ одним файлом (группа `monaco`); грузится
 * только в разделе «Код». Делить его нельзя: у модулей Monaco круговые зависимости, и при делении на части
 * редактор падает при загрузке.
 */
const MONACO_CHUNK = { name: 'monaco', limitKb: 4000 };

/**
 * Предупреждение о размере — своё, с отдельным порогом для файла Monaco: общий порог Vite один на все
 * файлы, а поднимать его ради Monaco — значит перестать замечать рост остальных.
 */
function chunkSizeGuard(): Plugin {
  return {
    name: 'portal-chunk-size',
    apply: 'build',
    generateBundle(_options, bundle) {
      for (const chunk of Object.values(bundle)) {
        if (chunk.type !== 'chunk') {
          continue;
        }
        const limit = chunk.name === MONACO_CHUNK.name ? MONACO_CHUNK.limitKb : CHUNK_LIMIT_KB;
        const size = new TextEncoder().encode(chunk.code).length / 1000;
        if (size > limit) {
          this.warn(`${chunk.fileName}: ${size.toFixed(0)} кБ — больше порога ${limit} кБ`);
        }
      }
    },
  };
}

/** Коды сообщений TypeScript, которые выдаёт проверка синтаксиса JavaScript: синтаксис, JSX, «только в TS». */
const SYNTAX_CODES = [
  [1000, 1999],
  [8000, 8999],
  [17000, 17999],
] as const;

/** Объявления библиотек клиента CoGIS для подсказок (контракт §7.5.16, А-128): файлы пакетов `@types`. */
const CLIENT_TYPES: Record<'jquery' | 'knockout' | 'leaflet', string[]> = {
  jquery: ['sizzle/index.d.ts', 'jquery/index.d.ts', 'jquery/JQueryStatic.d.ts', 'jquery/JQuery.d.ts', 'jquery/misc.d.ts', 'jquery/legacy.d.ts'],
  knockout: ['knockout/index.d.ts'],
  leaflet: ['geojson/index.d.ts', 'leaflet/index.d.ts'],
};

/**
 * Данные редактора, которые готовятся при сборке (концепция §5.13.19, Д-67): русские сообщения проверки
 * синтаксиса той же TypeScript, что в Monaco, — только нужные коды; объявления jQuery, Knockout и Leaflet —
 * без комментариев JSDoc: описания в них только английские, подсказки показывают имена и сигнатуры.
 */
function monacoData(): Plugin {
  const messages = 'virtual:ts-syntax-messages-ru';
  const libraries = 'virtual:cogis-client-types';
  return {
    name: 'portal-monaco-data',
    resolveId(id) {
      return id === messages || id === libraries ? `\0${id}` : null;
    },
    load(id) {
      if (id === `\0${messages}`) {
        const all = JSON.parse(readFileSync('node_modules/typescript/lib/ru/diagnosticMessages.generated.json', 'utf8')) as Record<string, string>;
        const kept = Object.fromEntries(
          Object.entries(all).filter(([key]) => {
            const code = Number(/_(\d+)$/.exec(key)?.[1]);
            return SYNTAX_CODES.some(([from, to]) => code >= from && code <= to);
          }),
        );
        return `export default ${JSON.stringify(kept)};`;
      }
      if (id === `\0${libraries}`) {
        const types = Object.fromEntries(
          Object.entries(CLIENT_TYPES).map(([library, files]) => [
            library,
            files.map((file) => ({
              path: `file:///node_modules/@types/${file}`,
              content: readFileSync(`node_modules/@types/${file}`, 'utf8').replace(/\/\*\*[\s\S]*?\*\//g, ''),
            })),
          ]),
        );
        return `export default ${JSON.stringify(types)};`;
      }
      return null;
    },
  };
}

/**
 * Только выбранные возможности Monaco (концепция §5.13.6, §5.13.19). Языки JavaScript и CSS создают воркер через
 * `internal/common/workers.js`, а тот побочными импортами подключает весь редактор: переход к определению,
 * переименование, форматирование, палитру команд — и раньше перевода, поэтому их подписи остаются английскими.
 * Побочные импорты из этого модуля убираются: нужные части подключает `src/code/monaco/setup.ts`.
 * Запасной способ создать воркер языка (`createWorker` в `workerManager.js`) тоже убирается: воркеры создаёт
 * `MonacoEnvironment.getWorker` из `setup.ts`, а запасной путь Vite собирал бы в лишний воркер TypeScript на 7 МБ.
 */
const MONACO_WORKERS = /monaco-editor\/esm\/vs\/internal\/common\/workers\.js$/;
const MONACO_WORKER_MANAGERS = /monaco-editor\/esm\/vs\/languages\/features\/[^/]+\/workerManager\.js$/;
const FALLBACK_WORKER = /^\s*createWorker: \(\) => new Worker\(new URL\([^\n]*\n/m;

function monacoSelective() {
  return {
    name: 'portal-monaco-selective',
    transform(code: string, id: string) {
      if (MONACO_WORKERS.test(id)) {
        return code.replace(/^import '[^']+';\n/gm, '');
      }
      if (MONACO_WORKER_MANAGERS.test(id)) {
        if (!FALLBACK_WORKER.test(code)) {
          throw new Error(`${id}: не найден запасной createWorker — проверьте после обновления Monaco`);
        }
        return code.replace(FALLBACK_WORKER, '');
      }
      return null;
    },
  };
}

export default defineConfig(({ mode }) => {
  const apiTarget = loadEnv(mode, '.', 'PORTAL_').PORTAL_API_TARGET;
  return {
    plugins: [react(), monacoData(), monacoSelective(), chunkSizeGuard()],
    // В режиме разработки Monaco собирается заранее: там тоже без лишних частей.
    optimizeDeps: { rolldownOptions: { plugins: [monacoSelective()] } },
    // Воркер TypeScript получает русские сообщения из того же модуля данных.
    worker: { plugins: () => [monacoData()] },
    server: {
      proxy: apiTarget ? { '/api': { target: apiTarget, changeOrigin: false } } : undefined,
    },
    build: {
      // Без встраивания `data:`: CSP портала их не пропускает, файлы своего origin — да (контракт §1.1, А-113).
      assetsInlineLimit: 0,
      // Размеры файлов проверяет `chunkSizeGuard`: порог Vite выключен, чтобы не дублировать предупреждение.
      chunkSizeWarningLimit: Number.POSITIVE_INFINITY,
      rolldownOptions: {
        output: {
          // Библиотека компонентов — отдельным файлом: меняется реже кода портала и дольше живёт в кэше браузера.
          codeSplitting: {
            groups: [
              // Помощник предзагрузки Vite нужен и основному файлу, и Monaco: без своей группы он попадает в файл
              // Monaco, и основной файл тянет Monaco при каждом открытии портала.
              { name: 'preload', test: /vite\/preload-helper/, priority: 4 },
              { name: MONACO_CHUNK.name, test: /node_modules\/monaco-editor\//, priority: 3 },
              { name: 'kontur-icons', test: /node_modules\/@skbkontur\/icons\//, priority: 2 },
              { name: 'kontur', test: /node_modules\/@skbkontur\//, priority: 1 },
            ],
          },
        },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      restoreMocks: true,
      // Тесты проверяют поведение, а не скорость: порог по умолчанию (5 с) даёт ложные падения,
      // когда параллельно идут сборка образов и сквозные тесты.
      testTimeout: 15000,
    },
  };
});
