/** Виды JS и HTML раздела «Код» через API (docs/portal-api.md §7.5.16–7.5.18). */
import { expect, type APIRequestContext } from '@playwright/test';

import type { CodeFile, CodeProject } from './code';
import { parseStream, type StreamEvent } from './portal';

export type WebKind = 'js_plugin' | 'html_widget' | 'catalog' | 'card_hooks';

/** Файл шаблона каждого веб-вида (§7.5.16). */
export const TEMPLATE_FILE: Record<WebKind, string> = {
  js_plugin: 'plugin.js',
  html_widget: 'widget.html',
  catalog: 'catalog.css',
  card_hooks: 'card-hooks.html',
};

export interface WebProject extends CodeProject {
  kind: WebKind | 'gptool';
  files: (CodeFile & { catalog: { insertion: string; clients: string[] } | null })[];
}

export async function createWebProject(api: APIRequestContext, kind: WebKind, name: string): Promise<WebProject> {
  const response = await api.post('/api/code/projects', { data: { source: 'template', kind, name } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).project;
}

/** «Проверить запрос» (§7.5.18): события потока после его завершения. */
export async function sqlCheck(
  api: APIRequestContext,
  projectId: string,
  sql: string,
  parameters: { name: string; value: string }[] = [],
): Promise<StreamEvent[]> {
  const response = await api.post(`/api/code/projects/${projectId}/sql-check`, { data: { sql, parameters }, timeout: 90_000 });
  expect(response.status(), await response.text()).toBe(200);
  return parseStream(await response.text());
}
