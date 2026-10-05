/** Действия с данными портала через API: документы базы знаний, диалоги, поток ответа. */
import { expect, type APIRequestContext, type APIResponse } from '@playwright/test';

import type { TestFile } from './files';

export interface StreamEvent {
  event: string;
  // Данные события — произвольный JSON по контракту (docs/portal-api.md §6).
  data: any; // eslint-disable-line @typescript-eslint/no-explicit-any
}

export interface KbDocument {
  id: string;
  title: string;
  status: string;
}

export type Knowledge = 'none' | 'shared' | 'shared_and_personal';

/** Разбирает завершённый поток событий (SSE) в список событий. */
export function parseStream(body: string): StreamEvent[] {
  return body
    .split('\n\n')
    .map((block) => {
      const event = /^event: (.+)$/m.exec(block)?.[1];
      const data = /^data: (.+)$/m.exec(block)?.[1];
      return event && data ? { event, data: JSON.parse(data) } : null;
    })
    .filter((item): item is StreamEvent => item !== null);
}

export function eventsOf(events: StreamEvent[], name: string): StreamEvent[] {
  return events.filter((item) => item.event === name);
}

/** Текст ответа, собранный из событий `delta`. */
export function answerText(events: StreamEvent[]): string {
  return eventsOf(events, 'delta')
    .map((item) => item.data.text as string)
    .join('');
}

export async function expectError(response: APIResponse, status: number, code: string): Promise<void> {
  const text = await response.text();
  expect(response.status(), text).toBe(status);
  expect(JSON.parse(text).error.code).toBe(code);
}

/** Загружает документ в базу знаний и ждёт, пока воркер его обработает. */
export async function addKbDocument(
  api: APIRequestContext,
  file: TestFile,
  options: { scope: 'shared' | 'personal'; cogis?: boolean },
): Promise<KbDocument> {
  const response = await api.post('/api/kb/documents', {
    multipart: { file, scope: options.scope, is_cogis: String(options.cogis ?? false) },
  });
  expect(response.status(), await response.text()).toBe(201);
  const document: KbDocument = await response.json();
  await waitKbReady(api, document.id);
  return document;
}

export async function waitKbReady(api: APIRequestContext, documentId: string): Promise<void> {
  await expect
    .poll(async () => (await (await api.get(`/api/kb/documents/${documentId}`)).json()).status, { timeout: 45_000 })
    .toBe('ready');
}

export async function createDialog(api: APIRequestContext, kind: 'chat' | 'sql' | 'cogis'): Promise<string> {
  const response = await api.post('/api/dialogs', { data: { kind } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).id;
}

/** Отправляет вопрос в чат и возвращает события потока после его завершения. */
export async function askChat(
  api: APIRequestContext,
  dialogId: string,
  content: string,
  options: { knowledge?: Knowledge; mode?: 'fast' | 'thorough'; attachmentIds?: string[] } = {},
): Promise<StreamEvent[]> {
  const response = await api.post(`/api/dialogs/${dialogId}/messages`, {
    data: {
      content,
      mode: options.mode ?? 'fast',
      knowledge: options.knowledge ?? 'none',
      ...(options.attachmentIds ? { attachment_ids: options.attachmentIds } : {}),
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  expect(response.headers()['content-type']).toContain('text/event-stream');
  return parseStream(await response.text());
}

/** Новый чат с одним вопросом и готовым ответом. */
export async function chatWithAnswer(
  api: APIRequestContext,
  content: string,
  options: { knowledge?: Knowledge } = {},
): Promise<{ dialogId: string; events: StreamEvent[] }> {
  const dialogId = await createDialog(api, 'chat');
  const events = await askChat(api, dialogId, content, options);
  expect(events.at(-1)?.event).toBe('done');
  return { dialogId, events };
}

/** Ответ заглушки ровно с таким текстом (docs/portal-api.md §12.2, правило 1). */
export function stubReply(text: string): string {
  return `[[stub:reply]]${text}[[/stub:reply]]`;
}
