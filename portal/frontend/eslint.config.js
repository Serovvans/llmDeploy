import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'e2e/test-results', 'e2e/playwright-report'] },
  js.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    extends: [...tseslint.configs.strict, reactHooks.configs.flat.recommended],
    languageOptions: { globals: globals.browser },
    rules: {
      // Клиент API — единственное место, где разрешён fetch (заголовок CSRF, разбор ошибок).
      'no-restricted-globals': ['error', { name: 'fetch', message: 'Запросы — только через src/api/client.ts.' }],
    },
  },
  { files: ['src/api/client.ts', 'src/**/*.test.{ts,tsx}', 'src/test/**'], rules: { 'no-restricted-globals': 'off' } },
  { files: ['public/**/*.js'], languageOptions: { globals: globals.browser, sourceType: 'script' } },
  { files: ['*.config.{js,ts}'], languageOptions: { globals: globals.node } },
);
