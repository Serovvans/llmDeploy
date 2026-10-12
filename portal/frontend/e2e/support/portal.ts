/** Действия с данными портала через API: документы базы знаний, диалоги, поток ответа. */
import { request as httpsRequest } from 'node:https';

import { expect, type APIRequestContext, type APIResponse } from '@playwright/test';

import type { TestFile } from './files';
import { BASE_URL } from './stand';

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

export type Scope = 'shared' | 'personal' | 'ministries';

/** Источник ответа (docs/portal-api.md §5.1): только поля, на которые опираются тесты. */
export interface Source {
  n: number;
  document_id: string;
  scope: Scope;
  page: number | null;
  timecode_seconds: number | null;
  image_id: string | null;
  fragment_id: string;
  quote: string;
}

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

/** Источники ответа из событий потока. */
export function sourcesOf(events: StreamEvent[]): Source[] {
  return eventsOf(events, 'sources').flatMap((event) => event.data.sources as Source[]);
}

/**
 * Поля формы загрузки документа. Теги — повторяемое поле: у Playwright оно задаётся
 * только через `FormData`.
 */
export function kbUploadForm(file: TestFile, options: { scope: Scope; cogis?: boolean; tags?: string[] }): FormData {
  const form = new FormData();
  form.append('file', new File([new Uint8Array(file.buffer)], file.name, { type: file.mimeType }));
  form.append('scope', options.scope);
  form.append('is_cogis', String(options.cogis ?? false));
  for (const tag of options.tags ?? []) {
    form.append('tags', tag);
  }
  return form;
}

/** Загружает документ в базу знаний и ждёт, пока воркер его обработает. */
export async function addKbDocument(
  api: APIRequestContext,
  file: TestFile,
  options: { scope: Scope; cogis?: boolean; tags?: string[] },
): Promise<KbDocument> {
  const response = await api.post('/api/kb/documents', { multipart: kbUploadForm(file, options) });
  expect(response.status(), await response.text()).toBe(201);
  const document: KbDocument = await response.json();
  await waitKbReady(api, document.id);
  return document;
}

/** Адрес загрузки записи в базу знаний: параметры — в строке запроса (docs/portal-api.md §1.6). */
export function kbMediaUrl(fileName: string, options: { scope: Scope; tags?: string[] }): string {
  const query = new URLSearchParams({ file_name: fileName, scope: options.scope });
  for (const tag of options.tags ?? []) {
    query.append('tags', tag);
  }
  return `/api/kb/documents/media?${query}`;
}

/** Загружает запись в базу знаний и ждёт конца расшифровки. */
export async function addKbRecording(
  api: APIRequestContext,
  file: TestFile,
  options: { scope: Scope; tags?: string[] },
): Promise<KbDocument> {
  const response = await api.post(kbMediaUrl(file.name, options), {
    data: file.buffer,
    headers: { 'Content-Type': 'application/octet-stream' },
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
  options: { collections?: Scope[]; tags?: string[]; mode?: 'fast' | 'thorough'; attachmentIds?: string[] } = {},
): Promise<StreamEvent[]> {
  const response = await api.post(`/api/dialogs/${dialogId}/messages`, {
    data: {
      content,
      mode: options.mode ?? 'fast',
      collections: options.collections ?? [],
      ...(options.tags ? { tags: options.tags } : {}),
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
  options: { collections?: Scope[]; tags?: string[] } = {},
): Promise<{ dialogId: string; events: StreamEvent[] }> {
  const dialogId = await createDialog(api, 'chat');
  const events = await askChat(api, dialogId, content, options);
  expect(events.at(-1)?.event).toBe('done');
  return { dialogId, events };
}

/** Загружает запись во вложение чата; расшифровка идёт в фоне. */
export async function attachRecording(api: APIRequestContext, dialogId: string, file: TestFile): Promise<APIResponse> {
  return api.post(`/api/dialogs/${dialogId}/attachments/media?file_name=${encodeURIComponent(file.name)}`, {
    data: file.buffer,
    headers: { 'Content-Type': 'application/octet-stream' },
  });
}

/** Ждёт конца расшифровки вложения и возвращает его: состояние `ready` или `error`. */
export async function waitTranscription(
  api: APIRequestContext,
  dialogId: string,
  attachmentId: string,
): Promise<{ transcription: { status: string; error_code: string | null }; duration_seconds: number | null }> {
  const address = `/api/dialogs/${dialogId}/attachments/${attachmentId}`;
  await expect
    .poll(async () => (await (await api.get(address)).json()).transcription.status, { timeout: 45_000 })
    .toMatch(/^(ready|error)$/);
  return (await api.get(address)).json();
}

/**
 * Загрузка записи, которая заявляет размер тела и шлёт нули, пока сервер не ответит: отказ по размеру
 * он обязан дать до чтения тела (docs/portal-api.md §1.6), и гигабайты для проверки не нужны.
 * Возвращает статус и тело ответа.
 */
export async function declareUpload(
  api: APIRequestContext,
  path: string,
  declaredBytes: number,
): Promise<{ status: number; body: string }> {
  const cookies = (await api.storageState()).cookies.map((cookie) => `${cookie.name}=${cookie.value}`).join('; ');
  return new Promise((resolve, reject) => {
    let answered = false;
    const upload = httpsRequest(
      new URL(path, BASE_URL),
      {
        method: 'POST',
        rejectUnauthorized: false,
        headers: { Cookie: cookies, 'X-Portal-Csrf': '1', 'Content-Length': String(declaredBytes) },
      },
      (response) => {
        answered = true;
        const chunks: Buffer[] = [];
        response.on('data', (chunk: Buffer) => chunks.push(chunk));
        response.on('end', () => {
          upload.destroy();
          resolve({ status: response.statusCode ?? 0, body: Buffer.concat(chunks).toString('utf8') });
        });
      },
    );
    // После ответа сервер закрывает соединение: ошибка записи остатка тела отказом теста не считается.
    upload.on('error', (error) => (answered ? undefined : reject(error)));
    const zeros = Buffer.alloc(64 * 1024);
    const pump = (): void => {
      while (!answered && upload.write(zeros)) {
        // Пишем, пока буфер соединения принимает.
      }
      if (!answered) {
        upload.once('drain', pump);
      }
    };
    pump();
  });
}

/** Сохраняет схему SQL-помощника и возвращает её идентификатор. */
export async function saveSqlSchema(api: APIRequestContext, name: string, content: string): Promise<string> {
  const response = await api.post('/api/sql/schemas', { data: { name, content } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).id;
}

/** Задаёт вопрос SQL-помощнику и возвращает события потока после его завершения. */
export async function askSql(
  api: APIRequestContext,
  dialogId: string,
  content: string,
  options: { schemaId?: string | null; testRun?: boolean } = {},
): Promise<StreamEvent[]> {
  const response = await api.post(`/api/dialogs/${dialogId}/messages`, {
    data: {
      content,
      action: 'write',
      dialect: 'postgres',
      schema_id: options.schemaId ?? null,
      test_run: options.testRun ?? false,
    },
  });
  expect(response.status(), await response.text()).toBe(200);
  return parseStream(await response.text());
}

/** Ответ заглушки ровно с таким текстом (docs/portal-api.md §12.2, правило 1). */
export function stubReply(text: string): string {
  return `[[stub:reply]]${text}[[/stub:reply]]`;
}
