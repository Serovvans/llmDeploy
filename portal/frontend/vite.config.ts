import react from '@vitejs/plugin-react';
import { loadEnv, type Plugin } from 'vite';
import { defineConfig } from 'vitest/config';

/** Порог размера файла сборки, кБ: файл библиотеки компонентов — около 1003 кБ и делению не поддаётся. */
const CHUNK_LIMIT_KB = 1050;
/**
 * Monaco с переводом — около 3,7 МБ одним файлом; грузится только в разделе «Код». Делить его нельзя:
 * у модулей Monaco круговые зависимости, и при делении на части редактор падает при загрузке.
 */
const MONACO_CHUNK = { module: '/src/code/monaco/setup.ts', limitKb: 4000 };

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
        const limit = chunk.facadeModuleId?.endsWith(MONACO_CHUNK.module) ? MONACO_CHUNK.limitKb : CHUNK_LIMIT_KB;
        const size = new TextEncoder().encode(chunk.code).length / 1000;
        if (size > limit) {
          this.warn(`${chunk.fileName}: ${size.toFixed(0)} кБ — больше порога ${limit} кБ`);
        }
      }
    },
  };
}

export default defineConfig(({ mode }) => {
  const apiTarget = loadEnv(mode, '.', 'PORTAL_').PORTAL_API_TARGET;
  return {
    plugins: [react(), chunkSizeGuard()],
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
