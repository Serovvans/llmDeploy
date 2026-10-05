import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

export default defineConfig(({ mode }) => {
  const apiTarget = loadEnv(mode, '.', 'PORTAL_').PORTAL_API_TARGET;
  return {
    plugins: [react()],
    server: {
      proxy: apiTarget ? { '/api': { target: apiTarget, changeOrigin: false } } : undefined,
    },
    build: {
      // Файл библиотеки компонентов — около 915 кБ (187 кБ в gzip) и делению не поддаётся;
      // порог чуть выше него, чтобы предупреждение сработало при заметном росте.
      chunkSizeWarningLimit: 950,
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
