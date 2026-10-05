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
  sql: {
    dialects: { id: string; title: string }[];
    default_dialect: string;
    schema_max_chars: number;
    max_schemas: number;
  };
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
  /** Только у диалога вида `docparse` в ответе `GET /api/dialogs/{id}`. */
  docparse?: DocparseResult;
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

export type SqlDanger = 'drop' | 'truncate' | 'delete_without_where' | 'update_without_where';

/** Проверка блоков `sql` ответа (контракт §7.1): `index` — номер блока среди блоков этого языка, с нуля. */
export interface SqlCheck {
  blocks: {
    index: number;
    /**
     * Строка текста ответа (с единицы, делитель — `\n`), на которой блок открывается: по ней итог привязан
     * к блоку. У сообщений, сохранённых до появления поля, его нет — запись показывается под текстом ответа.
     */
    line?: number;
    /** `null` — проверить не удалось, причина — в `unchecked`; опасные операции определяются при любом значении. */
    valid: boolean | null;
    unchecked?: string | null;
    error: { line: number; column: number; near: string } | null;
    dangers: SqlDanger[];
  }[];
}

/** Сообщение диалога (контракт §5.1). */
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
  /** Только у ответа в диалоге `sql`. */
  sql_check: SqlCheck | null;
  /** Только у вопроса в диалоге `sql`: опасные операции во вставленном запросе. */
  sql_dangers: SqlDanger[] | null;
  dropped_messages: number;
  created_at: string;
}

export type AnswerMode = 'fast' | 'thorough';

export type Knowledge = 'none' | 'shared' | 'shared_and_personal';

export type SqlAction = 'write' | 'explain' | 'debug' | 'optimize';
export type CogisAction = 'write' | 'explain' | 'debug';

/** Параметры вопроса по виду диалога (контракт §5.3); к ним добавляется `content`. */
export type QuestionParams =
  | { mode: AnswerMode; knowledge: Knowledge }
  | { action: SqlAction; dialect: string; schema_id: string | null }
  | { action: CogisAction }
  | Record<string, never>;

export interface SqlSchemaSummary {
  id: string;
  name: string;
  updated_at: string;
}

export interface SqlSchema extends SqlSchemaSummary {
  content: string;
}

export type SummaryStatus = 'streaming' | 'complete' | 'stopped' | 'length_limit' | 'error';

export interface DocparseField {
  title: string;
  value: string | null;
}

/** Результат разбора документа — поле `docparse` диалога (контракт §7.3). */
export interface DocparseResult {
  file_name: string;
  page_count: number | null;
  template_id: string;
  template_title: string;
  free_form: boolean;
  fields: DocparseField[];
  summary: string;
  summary_status: SummaryStatus;
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
