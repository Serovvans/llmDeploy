/**
 * Клиент API портала — единственное место, где вызывается `fetch`.
 * Здесь добавляется заголовок защиты от CSRF и разбирается общий формат ошибки (контракт §1.3, §2.2).
 */
import type {
  AdminUser,
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
  TemporaryPasswordResult,
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

type Method = 'GET' | 'POST' | 'PATCH';

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: ErrorBody };
    if (body.error && typeof body.error.code === 'string') {
      return new ApiError(response.status, body.error);
    }
  } catch {
    // Тело не в формате контракта (например, ответ прокси) — ниже общий отказ по статусу.
  }
  return new ApiError(response.status, {
    code: response.status === 503 ? 'service_unavailable' : 'internal_error',
    message: '',
  });
}

async function request<T>(method: Method, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (method !== 'GET') {
    headers['X-Portal-Csrf'] = '1';
  }
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
  }

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new NetworkError();
  }

  if (response.ok) {
    return (response.status === 204 ? undefined : await response.json()) as T;
  }

  const error = await parseError(response);
  if (error.code === 'unauthenticated' || error.code === 'login_step_required') {
    sessionSignalListener?.(error.code);
  }
  throw error;
}

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
  changeOwnPassword: (currentPassword: string, newPassword: string) =>
    request<undefined>('POST', '/api/auth/password', { new_password: newPassword, current_password: currentPassword }),

  startSecondFactorSetup: () => request<SecondFactorSetup>('POST', '/api/auth/second-factor/setup'),

  confirmSecondFactor: (code: string) =>
    request<SecondFactorConfirmResult>('POST', '/api/auth/second-factor/confirm', { code }),

  logout: () => request<undefined>('POST', '/api/auth/logout'),

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
};
