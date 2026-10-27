/** Модули данных редактора, которые готовит `vite.config.ts` (`monacoData`). */
declare module 'virtual:ts-syntax-messages-ru' {
  const messages: Record<string, string>;
  export default messages;
}

declare module 'virtual:cogis-client-types' {
  const types: Record<'jquery' | 'knockout' | 'leaflet', { path: string; content: string }[]>;
  export default types;
}

declare module 'monaco-editor/languages/features/typescript/lib/typescriptServices.js' {
  export const typescript: { setLocalizedDiagnosticMessages(messages: Record<string, string>): void };
}
