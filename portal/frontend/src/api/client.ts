/**
 * Клиент API портала — единственное место, где вызывается `fetch`.
 * Здесь добавляется заголовок защиты от CSRF и разбирается общий формат ошибки (контракт §1.3, §2.2).
 */
import { type DraftEvent, readEvents, type StreamEvent } from './stream';
import type {
  AdminUser,
  Attachment,
  CodeBuild,
  CodeCreated,
  CodeEdit,
  CodeEditFull,
  CodeFile,
  CodeProject,
  CodeProjectItem,
  CodeSdk,
  CodeSnapshot,
  CursorPage,
  Dialog,
  DialogKind,
  DialogSummary,
  DocumentText,
  KbDocument,
  KbScope,
  Memory,
  MemoryMode,
  Message,
  ErrorBody,
  FieldError,
  Page,
  PortalConfig,
  Role,
  SecondFactorConfirmResult,
  SecondFactorResult,
  SecondFactorSetup,
  Session,
  SortOrder,
  SqlSchema,
  SqlSchemaSummary,
  TemporaryPasswordResult,
  UnlockLoginResult,
  WizardForm,
} from './types';

/** Отказ сервера в формате контракта. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly fields: FieldError[];
  readonly details: Record<string, unknown>;

  constructor(status: number, body: ErrorBody) {
    super(body.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = body.code;
    this.fields = body.fields ?? [];
    this.details = body.details ?? {};
  }
}

/** Запрос не дошёл до портала или ответ не получен. */
export class NetworkError extends Error {
  constructor() {
    super('Нет связи с порталом');
    this.name = 'NetworkError';
  }
}

/** Связь оборвалась после статуса 200: вопрос сервером уже сохранён (контракт §5.5). */
export class StreamBrokenError extends NetworkError {
  constructor() {
    super();
    this.name = 'StreamBrokenError';
  }
}

export function isApiError(error: unknown, code?: string): error is ApiError {
  return error instanceof ApiError && (code === undefined || error.code === code);
}

/** Отказы, означающие, что состояние сессии в интерфейсе устарело. */
export type SessionSignal = 'unauthenticated' | 'login_step_required';

type SessionSignalListener = (signal: SessionSignal) => void;

let sessionSignalListener: SessionSignalListener | null = null;

/** Подписывает единственного слушателя (хранилище сессии); возвращает функцию отписки. */
export function onSessionSignal(listener: SessionSignalListener): () => void {
  sessionSignalListener = listener;
  return () => {
    if (sessionSignalListener === listener) {
      sessionSignalListener = null;
    }
  };
}

type Method = 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: ErrorBody };
    if (body.error && typeof body.error.code === 'string') {
      return new ApiError(response.status, body.error);
    }
  } catch {
    // Тело не в формате контракта (например, ответ прокси) — ниже общий отказ по статусу.
  }
  const codes: Record<number, string> = { 413: 'request_too_large', 503: 'service_unavailable' };
  return new ApiError(response.status, { code: codes[response.status] ?? 'internal_error', message: '' });
}

function notifySession(error: ApiError): void {
  if (error.code === 'unauthenticated' || error.code === 'login_step_required') {
    sessionSignalListener?.(error.code);
  }
}

interface SendOptions {
  json?: unknown;
  form?: FormData;
  accept?: string;
  signal?: AbortSignal;
}

/** Единственное место вызова `fetch`: заголовок CSRF, отказ в формате контракта, сигнал о потере сессии. */
async function send(method: Method, path: string, options: SendOptions = {}): Promise<Response> {
  const headers: Record<string, string> = { Accept: options.accept ?? 'application/json' };
  if (method !== 'GET') {
    headers['X-Portal-Csrf'] = '1';
  }
  if (options.json !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      signal: options.signal,
      body: options.form ?? (options.json === undefined ? undefined : JSON.stringify(options.json)),
    });
  } catch (error) {
    if (options.signal?.aborted) {
      throw error;
    }
    throw new NetworkError();
  }

  if (response.ok) {
    return response;
  }
  const error = await parseError(response);
  notifySession(error);
  throw error;
}

async function request<T>(method: Method, path: string, json?: unknown): Promise<T> {
  const response = await send(method, path, { json });
  return (response.status === 204 ? undefined : await response.json()) as T;
}

/**
 * Загрузка записи (контракт §1.6): тело запроса — сам файл, длину браузер называет сам. Через `XMLHttpRequest`,
 * а не `fetch`: только он сообщает ход отправки. Отказ — `ApiError`, обрыв — `NetworkError`,
 * отмена через `signal` — ошибка отмены (`isAbort`).
 */
function uploadBody<T>(path: string, file: File, onProgress: (percent: number) => void, signal: AbortSignal): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const settle = (finish: () => void) => {
      signal.removeEventListener('abort', abort);
      finish();
    };
    xhr.open('POST', path);
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.setRequestHeader('X-Portal-Csrf', '1');
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.floor((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () =>
      settle(() => {
        const response = new Response(xhr.responseText, { status: xhr.status });
        if (response.ok) {
          resolve(JSON.parse(xhr.responseText) as T);
          return;
        }
        void parseError(response).then((error) => {
          notifySession(error);
          reject(error);
        });
      });
    xhr.onerror = () => settle(() => reject(new NetworkError()));
    xhr.onabort = () => settle(() => reject(new DOMException('Загрузка отменена', 'AbortError')));
    signal.addEventListener('abort', abort);
    xhr.send(file);
  });
}

/** Открытые потоки ответа: их закрывают «Остановить», выход и добровольная смена пароля (концепция §5.12). */
const openStreams = new Set<AbortController>();

/** Закрывает все открытые потоки; ответы сохраняются на сервере как остановленные. */
export function closeOpenStreams(): void {
  for (const controller of openStreams) {
    controller.abort();
  }
}

/** Поток закрыт клиентом: «Остановить», выход, смена пароля. */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError';
}

/**
 * Открывает поток событий (контракт §6). Отказ до открытия потока — `ApiError`, как у обычного запроса.
 * Обрыв соединения после статуса 200 — `StreamBrokenError`; закрытие через `signal` — ошибка отмены (`isAbort`).
 * Событие `error` с кодом `session_ended` дополнительно сообщает о потере сессии, как отказ `unauthenticated`.
 */
async function* stream<E extends { type: string } = StreamEvent>(
  path: string,
  body: { json?: unknown; form?: FormData },
  signal: AbortSignal,
): AsyncGenerator<E> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort);
  openStreams.add(controller);
  try {
    const response = await send('POST', path, { ...body, accept: 'text/event-stream', signal: controller.signal });
    if (!response.body) {
      throw new StreamBrokenError();
    }
    let finished = false;
    try {
      for await (const event of readEvents<E>(response.body)) {
        if (event.type === 'error' && (event as { code?: unknown }).code === 'session_ended') {
          sessionSignalListener?.('unauthenticated');
        }
        finished = event.type === 'done' || event.type === 'error';
        yield event;
      }
    } catch {
      throw controller.signal.aborted ? new DOMException('Поток закрыт', 'AbortError') : new StreamBrokenError();
    }
    // Поток закрылся без завершающего события — тот же обрыв связи.
    if (!finished) {
      throw new StreamBrokenError();
    }
  } finally {
    openStreams.delete(controller);
    signal.removeEventListener('abort', abort);
  }
}

/** Файл ответа и его имя из `Content-Disposition` (`filename*=UTF-8''…`, контракт §5.9, §7.5.5). */
async function download(response: Response): Promise<{ blob: Blob; fileName: string | null }> {
  const name = /filename\*=UTF-8''([^;]+)/i.exec(response.headers.get('Content-Disposition') ?? '')?.[1];
  return { blob: await response.blob(), fileName: name ? decodeURIComponent(name) : null };
}

const ZIP_TYPE = 'application/zip';

export interface UserListQuery {
  page: number;
  q: string;
  order: SortOrder;
}

export const USERS_PAGE_SIZE = 50;

export const api = {
  getSession: () => request<Session>('GET', '/api/auth/session'),

  login: (login: string, password: string) => request<Session>('POST', '/api/auth/login', { login, password }),

  submitCode: (code: string) => request<SecondFactorResult>('POST', '/api/auth/second-factor', { code }),

  submitBackupCode: (backupCode: string) =>
    request<SecondFactorResult>('POST', '/api/auth/second-factor', { backup_code: backupCode }),

  /** Обязательная смена пароля (шаг `password_change`): сервер выдаёт новую сессию. */
  changeTemporaryPassword: (newPassword: string) =>
    request<Session>('POST', '/api/auth/password', { new_password: newPassword }),

  /** Добровольная смена пароля (шаг `ready`): сервер гасит все сессии и новой не выдаёт. */
  changeOwnPassword: (currentPassword: string, newPassword: string) => {
    closeOpenStreams();
    return request<undefined>('POST', '/api/auth/password', {
      new_password: newPassword,
      current_password: currentPassword,
    });
  },

  startSecondFactorSetup: () => request<SecondFactorSetup>('POST', '/api/auth/second-factor/setup'),

  confirmSecondFactor: (code: string) =>
    request<SecondFactorConfirmResult>('POST', '/api/auth/second-factor/confirm', { code }),

  logout: () => {
    closeOpenStreams();
    return request<undefined>('POST', '/api/auth/logout');
  },

  getConfig: () => request<PortalConfig>('GET', '/api/config'),

  listUsers: ({ page, q, order }: UserListQuery) => {
    const params = new URLSearchParams({
      page: String(page),
      page_size: String(USERS_PAGE_SIZE),
      sort: 'full_name',
      order,
    });
    if (q) {
      params.set('q', q);
    }
    return request<Page<AdminUser>>('GET', `/api/admin/users?${params.toString()}`);
  },

  createUser: (user: { full_name: string; login: string; role: Role }) =>
    request<TemporaryPasswordResult>('POST', '/api/admin/users', user),

  updateUser: (id: string, changes: { full_name?: string; role?: Role }) =>
    request<AdminUser>('PATCH', `/api/admin/users/${id}`, changes),

  resetUserPassword: (id: string) => request<TemporaryPasswordResult>('POST', `/api/admin/users/${id}/reset-password`),

  resetUserSecondFactor: (id: string) => request<AdminUser>('POST', `/api/admin/users/${id}/reset-second-factor`),

  blockUser: (id: string) => request<AdminUser>('POST', `/api/admin/users/${id}/block`),

  unblockUser: (id: string) => request<AdminUser>('POST', `/api/admin/users/${id}/unblock`),

  unlockUserLogin: (id: string) => request<UnlockLoginResult>('POST', `/api/admin/users/${id}/unlock-login`),

  listDialogs: (kind: DialogKind, cursor: string | null) => {
    const params = new URLSearchParams({ kind });
    if (cursor) {
      params.set('cursor', cursor);
    }
    return request<CursorPage<Dialog>>('GET', `/api/dialogs?${params.toString()}`);
  },

  /** `carried` — перенос (контракт §5.10.7): текст и исходный диалог; только для `chat`, `sql`, `cogis`. */
  createDialog: (kind: DialogKind, carried?: { text: string; source_dialog_id: string }) =>
    request<Dialog>('POST', '/api/dialogs', carried ? { kind, carried } : { kind }),

  getDialog: (id: string) => request<Dialog>('GET', `/api/dialogs/${id}`),

  renameDialog: (id: string, title: string) => request<Dialog>('PATCH', `/api/dialogs/${id}`, { title }),

  deleteDialog: (id: string) => request<undefined>('DELETE', `/api/dialogs/${id}`),

  /** Сообщения от новых к старым (контракт §5.2). */
  listMessages: (id: string, cursor: string | null) =>
    request<CursorPage<Message>>(
      'GET',
      `/api/dialogs/${id}/messages${cursor ? `?${new URLSearchParams({ cursor }).toString()}` : ''}`,
    ),

  /**
   * Оценка памяти диалога (контракт §5.10.3). `query` — параметры следующего вопроса, от которых зависит
   * место: в чате `mode` и `search`, в SQL `action`, `dialect`, `schema_id`, в CoGIS `action`.
   */
  getMemory: (id: string, query: Record<string, string>) => {
    const params = new URLSearchParams(query).toString();
    return request<Memory>('GET', `/api/dialogs/${id}/memory${params ? `?${params}` : ''}`);
  },

  /** Режим пары; `questionId` — вопрос этого диалога (контракт §5.10.4). */
  setMemoryMode: (id: string, questionId: string, mode: MemoryMode) =>
    request<Message>('PATCH', `/api/dialogs/${id}/messages/${questionId}`, { memory_mode: mode }),

  /**
   * Черновик пересказа потоком (контракт §5.10.5, §6.5): сжатие или перенос (с `until_message_id` —
   * «Продолжить отсюда»). Сервер ничего не сохраняет; закрытие потока через `signal` — отмена.
   */
  draftSummary: (
    id: string,
    body: { purpose: 'compress' } | { purpose: 'transfer'; until_message_id?: string | null },
    signal: AbortSignal,
  ) => stream<DraftEvent>(`/api/dialogs/${id}/memory/draft`, { json: body }, signal),

  /** «Применить» черновик сжатия и «Изменить» пересказ (контракт §5.10.6). */
  saveSummary: (id: string, body: { text: string; until_message_id: string }) =>
    request<DialogSummary>('PUT', `/api/dialogs/${id}/memory/summary`, body),

  /** «Отменить сжатие» (контракт §5.10.6). */
  deleteSummary: (id: string) => request<undefined>('DELETE', `/api/dialogs/${id}/memory/summary`),

  /** Вопрос в диалоге любого вида: `content` и параметры этого вида (контракт §5.3). */
  sendMessage: (id: string, body: { content: string } & Record<string, unknown>, signal: AbortSignal) =>
    stream(`/api/dialogs/${id}/messages`, { json: body }, signal),

  /** `body` — только для `sql`: `{ schema_id }` взамен удалённой схемы вопроса (контракт §5.6). */
  regenerate: (id: string, signal: AbortSignal, body?: { schema_id: string | null }) =>
    stream(`/api/dialogs/${id}/regenerate`, { json: body }, signal),

  /** Файл DOCX: диалог целиком или один ответ `messageId` (контракт §5.9). */
  exportDialog: async (id: string, messageId?: string) => {
    const query = messageId ? `?${new URLSearchParams({ message_id: messageId }).toString()}` : '';
    return download(await send('GET', `/api/dialogs/${id}/export${query}`, { accept: DOCX_TYPE }));
  },

  listSqlSchemas: () => request<{ items: SqlSchemaSummary[] }>('GET', '/api/sql/schemas'),

  getSqlSchema: (id: string) => request<SqlSchema>('GET', `/api/sql/schemas/${id}`),

  createSqlSchema: (schema: { name: string; content: string }) => request<SqlSchema>('POST', '/api/sql/schemas', schema),

  updateSqlSchema: (id: string, schema: { name: string; content: string }) =>
    request<SqlSchema>('PUT', `/api/sql/schemas/${id}`, schema),

  deleteSqlSchema: (id: string) => request<undefined>('DELETE', `/api/sql/schemas/${id}`),

  /** Есть ли в общей базе готовая документация CoGIS (контракт §8.1). */
  getCogisDocumentation: () => request<{ available: boolean }>('GET', '/api/kb/cogis-documentation'),

  /** Разбор документа: поток `progress` → `extraction` → `delta` → `done` (контракт §6.4). */
  startDocparse: (file: File, templateId: string, signal: AbortSignal) => {
    const form = new FormData();
    form.append('file', file);
    form.append('template_id', templateId);
    return stream('/api/docparse', { form }, signal);
  },

  uploadAttachment: async (dialogId: string, file: File) => {
    const form = new FormData();
    form.append('file', file);
    const response = await send('POST', `/api/dialogs/${dialogId}/attachments`, { form });
    return (await response.json()) as Attachment;
  },

  /** Запись (аудио, видео) во вложении: `onProgress` — ход отправки в процентах (контракт §5.8). */
  uploadMediaAttachment: (dialogId: string, file: File, onProgress: (percent: number) => void, signal: AbortSignal) =>
    uploadBody<Attachment>(
      `/api/dialogs/${dialogId}/attachments/media?${new URLSearchParams({ file_name: file.name }).toString()}`,
      file,
      onProgress,
      signal,
    ),

  /** Состояние вложения: расшифровка записи узнаётся опросом (контракт §5.8). */
  getAttachment: (dialogId: string, attachmentId: string) =>
    request<Attachment>('GET', `/api/dialogs/${dialogId}/attachments/${attachmentId}`),

  deleteAttachment: (dialogId: string, attachmentId: string) =>
    request<undefined>('DELETE', `/api/dialogs/${dialogId}/attachments/${attachmentId}`),

  listKbDocuments: ({ scope, page, q, tags, sort, order }: KbListQuery) => {
    const params = new URLSearchParams({ scope, page: String(page), page_size: String(KB_PAGE_SIZE), sort, order });
    if (q) {
      params.set('q', q);
    }
    tags.forEach((tag) => params.append('tags', tag));
    return request<Page<KbDocument>>('GET', `/api/kb/documents?${params.toString()}`);
  },

  uploadKbDocument: async (file: File, target: KbUploadTarget & { isCogis: boolean }, signal: AbortSignal) => {
    const form = new FormData();
    form.append('file', file);
    form.append('scope', target.scope);
    form.append('is_cogis', String(target.isCogis));
    target.tags.forEach((tag) => form.append('tags', tag));
    const response = await send('POST', '/api/kb/documents', { form, signal });
    return (await response.json()) as KbDocument;
  },

  /** Запись (аудио, видео) в базу знаний: тело — сам файл, остальное — в строке запроса (контракт §8.1). */
  uploadKbMedia: (file: File, target: KbUploadTarget, onProgress: (percent: number) => void, signal: AbortSignal) => {
    const params = new URLSearchParams({ file_name: file.name, scope: target.scope });
    target.tags.forEach((tag) => params.append('tags', tag));
    return uploadBody<KbDocument>(`/api/kb/documents/media?${params.toString()}`, file, onProgress, signal);
  },

  /** Заменяет список тегов документа целиком; `[]` — снять все (контракт §8.5). */
  setKbDocumentTags: (id: string, tags: string[]) => request<KbDocument>('PUT', `/api/kb/documents/${id}/tags`, { tags }),

  getKbDocument: (id: string) => request<KbDocument>('GET', `/api/kb/documents/${id}`),

  deleteKbDocument: (id: string) => request<undefined>('DELETE', `/api/kb/documents/${id}`),

  retryKbDocument: (id: string) => request<KbDocument>('POST', `/api/kb/documents/${id}/retry`),

  /**
   * Текст страницы: по номеру `page`, иначе страница фрагмента `fragmentId`, иначе у записи — часть,
   * в которую попадает момент `timecode`, иначе первая (контракт §8.3).
   */
  getKbDocumentText: (id: string, target: { page?: number; fragmentId?: string; timecode?: number }) => {
    const params = new URLSearchParams();
    if (target.page !== undefined) {
      params.set('page', String(target.page));
    }
    if (target.fragmentId) {
      params.set('fragment_id', target.fragmentId);
    }
    if (target.timecode !== undefined) {
      params.set('timecode', String(target.timecode));
    }
    const query = params.toString();
    return request<DocumentText>('GET', `/api/kb/documents/${id}/text${query ? `?${query}` : ''}`);
  },

  // ---------- Раздел «Код» (контракт §7.5, 0.9) ----------

  listCodeProjects: (cursor: string | null) => {
    const params = new URLSearchParams();
    if (cursor) {
      params.set('cursor', cursor);
    }
    const query = params.toString();
    return request<CursorPage<CodeProjectItem>>('GET', `/api/code/projects${query ? `?${query}` : ''}`);
  },

  /** Мастер (с `carried` — перенос в «Код», §7.5.4) или пример из инструкции. */
  createCodeProject: (
    body:
      | { source: 'wizard'; wizard: WizardForm; carried?: { text: string; source_dialog_id: string } }
      | { source: 'example'; name: string },
  ) => request<CodeCreated>('POST', '/api/code/projects', body),

  importCodeProject: async (file: File, name: string) => {
    const form = new FormData();
    form.append('file', file);
    form.append('name', name);
    const response = await send('POST', '/api/code/projects/import', { form });
    return (await response.json()) as CodeCreated;
  },

  getCodeProject: (id: string) => request<CodeProject>('GET', `/api/code/projects/${id}`),

  updateCodeProject: (id: string, changes: { packages?: string[]; schema_id?: string | null }) =>
    request<CodeProject>('PATCH', `/api/code/projects/${id}`, changes),

  deleteCodeProject: (id: string) => request<undefined>('DELETE', `/api/code/projects/${id}`),

  createCodeFile: (projectId: string, path: string) =>
    request<CodeFile>('POST', `/api/code/projects/${projectId}/files`, { path, content: '' }),

  /** Сохранение и переименование с ревизией, которую видел сотрудник (контракт §7.5.3). */
  updateCodeFile: (projectId: string, fileId: string, changes: { revision: number; path?: string; content?: string }) =>
    request<CodeFile>('PATCH', `/api/code/projects/${projectId}/files/${fileId}`, changes),

  deleteCodeFile: (projectId: string, fileId: string) =>
    request<undefined>('DELETE', `/api/code/projects/${projectId}/files/${fileId}`),

  /** «Скачать для Visual Studio» — ZIP исходников с файлом проекта (§7.5.5). */
  exportCodeProject: async (id: string) =>
    download(await send('GET', `/api/code/projects/${id}/export`, { accept: ZIP_TYPE })),

  listCodeSnapshots: (id: string) => request<{ items: CodeSnapshot[] }>('GET', `/api/code/projects/${id}/snapshots`),

  restoreCodeSnapshot: (id: string, snapshotId: string) =>
    request<CodeProject>('POST', `/api/code/projects/${id}/snapshots/${snapshotId}/restore`),

  /** Сборка синхронная: ответ приходит, когда она закончена (контракт §7.5.7). */
  buildCodeProject: (id: string) => request<CodeBuild>('POST', `/api/code/projects/${id}/build`),

  /** Архив для CoGIS (§7.5.7); отказ `409 archive_unavailable` с `details.reason`. */
  downloadCodeArchive: async (id: string) =>
    download(await send('GET', `/api/code/projects/${id}/build/archive`, { accept: ZIP_TYPE })),

  getCodeEdit: (id: string, messageId: string) =>
    request<CodeEditFull>('GET', `/api/code/projects/${id}/edits/${messageId}`),

  /** Решение по правке: принимаемые файлы с ревизией, с которой сравнивал сотрудник; `[]` — отклонить всё. */
  decideCodeEdit: (id: string, messageId: string, accept: { path: string; revision: number | null }[]) =>
    request<{ edit: CodeEdit; project: CodeProject }>('POST', `/api/code/projects/${id}/edits/${messageId}/decision`, {
      accept,
    }),

  /**
   * Справочник и набор пакетов (§7.5.14). Единственный маршрут с `ETag`: запрос — обычный, без
   * `cache: "no-store"`, поэтому повторное открытие раздела получает `304` из кеша браузера.
   */
  getCodeSdk: () => request<CodeSdk>('GET', '/api/code/sdk'),

  /** Подсказки тегов: теги документов выбранных коллекций, видимых пользователю (контракт §8.5). */
  listKbTags: (scopes: KbScope[], q: string) => {
    const params = new URLSearchParams(scopes.map((scope) => ['scope', scope]));
    if (q) {
      params.set('q', q);
    }
    return request<{ items: string[] }>('GET', `/api/kb/tags?${params.toString()}`);
  },
};

const DOCX_TYPE = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document';

export const KB_PAGE_SIZE = 50;

export interface KbListQuery {
  scope: KbScope;
  page: number;
  q: string;
  /** Отбор: документы, у которых есть хотя бы один из тегов. */
  tags: string[];
  sort: 'created_at' | 'title';
  order: SortOrder;
}

/** Куда и с какими тегами добавляется файл базы знаний. */
export interface KbUploadTarget {
  scope: KbScope;
  tags: string[];
}

/** Адрес оригинала документа базы знаний; `page` — открыть PDF на странице (контракт §8.2). */
export function kbFileUrl(documentId: string, page: number | null): string {
  return `/api/kb/documents/${documentId}/file${page === null ? '' : `#page=${page}`}`;
}

/** Адрес файла вложения на портале: оригинал и миниатюра изображения (контракт §5.8). */
export function attachmentFileUrl(dialogId: string, attachmentId: string): string {
  return `/api/dialogs/${dialogId}/attachments/${attachmentId}/file`;
}

/** Адрес картинки документа: его строит интерфейс по источнику, в тексте ответа адресов нет (контракт §8.6). */
export function kbImageUrl(documentId: string, imageId: string): string {
  return `/api/kb/documents/${documentId}/images/${imageId}`;
}

/** Адрес сжатого звука записи для проигрывателя (контракт §8.7). */
export function kbAudioUrl(documentId: string): string {
  return `/api/kb/documents/${documentId}/audio`;
}
