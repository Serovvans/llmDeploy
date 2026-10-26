/** Единственное, что `vite.config.ts` берёт из Node: чтение файлов пакетов при сборке (без пакета `@types/node`). Лежит в `src`, потому что образ копирует только её и конфигурацию. */
declare module 'node:fs' {
  export function readFileSync(path: string, encoding: 'utf8'): string;
}
