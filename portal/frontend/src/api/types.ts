/** Типы контракта `docs/portal-api.md` (§1.3, §1.4, §2.3, §2.4, §3, §4). Имена полей — как в контракте. */

export type LoginStep = 'second_factor' | 'password_change' | 'second_factor_setup' | 'ready';

export type Role = 'admin' | 'employee';

export interface SessionUser {
  id: string;
  login: string;
  full_name: string;
  role: Role;
  second_factor_configured: boolean;
  backup_codes: { remaining: number; total: number } | null;
}

export interface Session {
  step: LoginStep;
  user: SessionUser | null;
}

export interface SecondFactorResult {
  session: Session;
  backup_code_used: boolean;
}

export interface SecondFactorSetup {
  secret: string;
  qr: { size: number; path: string };
}

export interface SecondFactorConfirmResult {
  session: Session;
  backup_codes: string[];
}

export type AdminUserState = 'active' | 'never_logged_in' | 'blocked';

export interface AdminUser {
  id: string;
  login: string;
  full_name: string;
  role: Role;
  state: AdminUserState;
  second_factor_configured: boolean;
  is_me: boolean;
  created_at: string;
}

export interface TemporaryPasswordResult {
  user: AdminUser;
  temporary_password: string;
}

export interface Page<T> {
  items: T[];
  page: number;
  page_size: number;
  total: number;
}

export type SortOrder = 'asc' | 'desc';

export interface PortalConfig {
  password: { min_length: number; max_length: number };
  dialogs: { message_max_chars: number };
  chat: {
    attachment_max_bytes: number;
    attachment_max_pages: number;
    attachment_extensions: string[];
    max_attachments: number;
    max_images: number;
  };
  kb: { document_max_bytes: number; document_max_pages: number; document_extensions: string[] };
  docparse: {
    document_max_bytes: number;
    max_pages: number;
    document_extensions: string[];
    templates: { id: string; title: string; description: string; free_form: boolean }[];
  };
  sql: { dialects: { id: string; title: string }[]; default_dialect: string; schema_max_chars: number };
}

export interface FieldError {
  field: string;
  code: string;
  message: string;
}

export interface ErrorBody {
  code: string;
  message: string;
  fields?: FieldError[];
  details?: Record<string, unknown>;
}

/** Список с подгрузкой по курсору (контракт §1.4). */
export interface CursorPage<T> {
  items: T[];
  next_cursor: string | null;
}

export type DialogKind = 'chat' | 'sql' | 'cogis' | 'docparse';

export interface Dialog {
  id: string;
  kind: DialogKind;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface Attachment {
  id: string;
  file_name: string;
  media_type: string;
  page_count: number | null;
  image_count: number;
  created_at: string;
}

export type MessageStatus = 'complete' | 'streaming' | 'stopped' | 'length_limit' | 'error';

export type KbScope = 'shared' | 'personal';

/** Источник ответа (контракт §5.1): `n` — номер сноски `[n]` в тексте. */
export interface Source {
  n: number;
  document_id: string;
  document_title: string;
  scope: KbScope;
  page: number | null;
  fragment_id: string;
  quote: string;
}

/** Сообщение диалога (контракт §5.1). Поля проверки SQL понадобятся на этапе 5. */
export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status: MessageStatus;
  error_code: string | null;
  reasoning: string | null;
  reasoning_seconds: number | null;
  attachments: Attachment[];
  /** `null` — поиск в базе знаний не выполнялся; `[]` — выполнялся, сносок в ответе нет. */
  sources: Source[] | null;
  sources_found: number | null;
  dropped_messages: number;
  created_at: string;
}

export type AnswerMode = 'fast' | 'thorough';

export type Knowledge = 'none' | 'shared' | 'shared_and_personal';

/** Тело сообщения чата (контракт §5.3). */
export interface ChatMessageBody {
  content: string;
  attachment_ids: string[];
  mode: AnswerMode;
  knowledge: Knowledge;
}

export type KbStatus = 'queued' | 'processing' | 'ready' | 'error';

/** Документ базы знаний (контракт §8.1). */
export interface KbDocument {
  id: string;
  title: string;
  scope: KbScope;
  is_cogis: boolean;
  author: { full_name: string; is_me: boolean };
  created_at: string;
  page_count: number | null;
  status: KbStatus;
  error_code: string | null;
  progress: { pages_done: number; pages_total: number; recognizing: boolean } | null;
  can_delete: boolean;
}

/** Текст страницы с цитатой отрезками (контракт §8.3). */
export interface DocumentText {
  page: number | null;
  page_count: number | null;
  recognized: boolean;
  segments: { text: string; highlight: boolean }[];
}

/** Существующий документ в отказе `duplicate_document` (контракт §8.1). */
export interface DuplicateDocument {
  id: string;
  title: string;
  author_full_name: string;
  created_at: string;
  status: KbStatus;
  error_code: string | null;
  can_delete: boolean;
}
