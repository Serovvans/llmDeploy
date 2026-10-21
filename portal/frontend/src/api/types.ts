/** Типы контракта `docs/portal-api.md` версии 0.17 (поля релизов 0.7 и 0.8). Имена полей — как в контракте. */

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
  /** Временная блокировка входа после серии неудач: время окончания (ISO 8601, UTC) или `null`. */
  login_locked_until: string | null;
  is_me: boolean;
  created_at: string;
}

/** Ответ `unlock-login`: `unlocked` равно `false`, если блокировки уже не было. */
export interface UnlockLoginResult {
  user: AdminUser;
  unlocked: boolean;
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
  kb: {
    document_max_bytes: number;
    document_max_pages: number;
    document_extensions: string[];
    tags: { max_per_document: number; max_length: number; max_selected: number };
  };
  /** Записи (аудио и видео): при `enabled: false` интерфейс их не предлагает; остальное — пределы для подсказки. */
  media: {
    enabled: boolean;
    max_bytes: number;
    max_minutes: number;
    chat_max_minutes: number;
    extensions: string[];
  };
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
    /** Проверка на тестовых данных: при `enabled: false` переключателя нет; `dialect` — единственный проверяемый. */
    test_run: { enabled: boolean; dialect: string };
  };
  /**
   * «Память диалога» (контракт §4): при `enabled: false` нет индикатора, панели, сжатия и переноса.
   * `keep_recent_pairs` — только для текстов «кроме последних двух…»: что войдёт в сжатие, отдаёт оценка;
   * `max_chars` — предел длины пересказа и перенесённого.
   */
  memory: { enabled: boolean; keep_recent_pairs: number; max_chars: number };
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
  /** Только в ответе `GET /api/dialogs/{id}`: применённый пересказ; `null` — сжатия нет (контракт §5.1). */
  summary?: DialogSummary | null;
  /** Только в ответе `GET /api/dialogs/{id}`: перенесённое при создании; `null` — диалог создан не переносом. */
  carried?: DialogCarried | null;
}

/** Применённый пересказ: `until_message_id` — ответ, которым кончается пересказанная часть; `messages` — сообщений до него включительно. */
export interface DialogSummary {
  text: string;
  until_message_id: string;
  messages: number;
}

/** Перенесённое (контракт §5.1): источник `null` — он удалён, ссылки нет; `source_title` `null` — у источника не было названия. */
export interface DialogCarried {
  text: string;
  source_dialog_id: string | null;
  source_kind: DialogKind | null;
  source_title: string | null;
}

export type TranscriptionStatus = 'queued' | 'processing' | 'ready' | 'error';

export interface Attachment {
  id: string;
  file_name: string;
  media_type: string;
  page_count: number | null;
  image_count: number;
  /** Длительность записи; `null`, пока она не измерена, и у остальных вложений. */
  duration_seconds: number | null;
  /** `null` у всего, кроме записи (аудио, видео); `error_code` — только при `status: "error"`. */
  transcription: { status: TranscriptionStatus; error_code: string | null } | null;
  created_at: string;
}

export type MessageStatus = 'complete' | 'streaming' | 'stopped' | 'length_limit' | 'error';

export type KbScope = 'shared' | 'personal' | 'ministries';

/** Источник ответа (контракт §5.1): `n` — номер сноски `[n]` в тексте. */
export interface Source {
  n: number;
  document_id: string;
  document_title: string;
  scope: KbScope;
  page: number | null;
  /** У фрагмента расшифровки записи — секунда, с которой он начинается; иначе `null`. */
  timecode_seconds: number | null;
  /** У источника-картинки; `quote` тогда — её описание. Адрес картинки строит интерфейс. */
  image_id: string | null;
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

/** Таблица тестовых данных или результат запроса: ячейки — строки, как их отдаёт СУБД, либо `null` (SQL `NULL`). */
export interface SqlRunRows {
  columns: string[];
  rows: (string | null)[][];
  /** Строк было больше, чем отдано. */
  truncated: boolean;
}

/**
 * Состояние проверки блока на тестовых данных (контракт §7.4). `pending` и `running` бывают только
 * в событиях; значение вне перечня интерфейс показывает как «Не проверялось».
 */
export type SqlRunStatus =
  | 'pending'
  | 'running'
  | 'passed'
  | 'fixed'
  | 'failed'
  | 'not_checked'
  | 'unavailable'
  | 'timeout'
  | 'stopped';

export interface SqlRunBlock {
  index: number;
  /** Как в `SqlCheck`: строка ответа, на которой блок открывается. */
  line: number;
  status: SqlRunStatus;
  /** Почему блок не проверялся — только при `status: "not_checked"`. */
  reason: string | null;
  fixture: ({ table: string } & SqlRunRows)[] | null;
  result: SqlRunRows | null;
  affected_rows: number | null;
  /** Сообщение СУБД: при `failed` — последняя ошибка, при `fixed` — ошибка исходного запроса. */
  error: string | null;
  /** Исправленный запрос — только при `status: "fixed"`. */
  fixed_sql: string | null;
}

/** Проверка блоков `sql` ответа на тестовых данных — событие `sql_run` и поле `Message.sql_run` (контракт §7.4). */
export interface SqlRun {
  blocks: SqlRunBlock[];
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
  /** Только у ответа на вопрос с `test_run: true`; `null` — проверки на тестовых данных нет. */
  sql_run: SqlRun | null;
  /** Только у вопроса в диалоге `sql`: опасные операции во вставленном запросе. */
  sql_dangers: SqlDanger[] | null;
  dropped_messages: number;
  /** Режим пары «вопрос + ответ» (контракт §5.10.1): у вопроса — сохранённый, у ответа — режим его вопроса. */
  memory_mode: MemoryMode;
  created_at: string;
}

/** «Учитывать», «Без вложений», «Не учитывать» (контракт §5.10.1). */
export type MemoryMode = 'full' | 'text_only' | 'excluded';

/** Уровень заполненности памяти диалога: считает сервер, интерфейс сам не вычисляет (контракт §5.10.3). */
export type MemoryLevel = 'free' | 'filling' | 'almost_full' | 'overflow';

/** Пара «вопрос + ответ» в оценке памяти; номер пары — её место в списке с 1. */
export interface MemoryPair {
  question_id: string;
  answer_id: string;
  /** Первые 200 символов вопроса; пустая строка — вопрос из одних вложений. */
  question_preview: string;
  answer_preview: string;
  attachments: number;
  memory_mode: MemoryMode;
  tokens: number;
  /** Пара не поместится в следующий запрос при текущем выборе. */
  dropped: boolean;
  /** Пара не дальше границы пересказа: модель получает вместо неё пересказ. */
  summarized: boolean;
}

/** Оценка заполненности памяти диалога — `GET /api/dialogs/{id}/memory` (контракт §5.10.3). */
export interface Memory {
  level: MemoryLevel;
  /** Только для подсказки; может быть больше 100. */
  percent: number;
  used_tokens: number;
  limit_tokens: number;
  /** Номера пар, которые сожмёт следующее «Сжать раннюю часть»; `null` — сжимать нечего. Интерфейс сам не вычисляет. */
  compress_pairs: { from: number; to: number } | null;
  pairs: MemoryPair[];
}

export type AnswerMode = 'fast' | 'thorough';

export type SqlAction = 'write' | 'explain' | 'debug' | 'optimize';
export type CogisAction = 'write' | 'explain' | 'debug';

/** Параметры вопроса по виду диалога (контракт §5.3); к ним добавляется `content`. */
export type QuestionParams =
  | { mode: AnswerMode; collections: KbScope[]; tags?: string[] }
  | { action: SqlAction; dialect: string; schema_id: string | null; test_run: boolean }
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
  /** `recording` — запись (аудио или видео). */
  kind: 'document' | 'recording';
  is_cogis: boolean;
  tags: string[];
  author: { full_name: string; is_me: boolean };
  created_at: string;
  page_count: number | null;
  /** Длительность записи; `null`, пока она не измерена, и у документов. */
  duration_seconds: number | null;
  status: KbStatus;
  error_code: string | null;
  /** Только при `status: "processing"`: у документа — страницы, у записи — расшифрованные секунды. */
  progress:
    | { pages_done: number; pages_total: number; recognizing: boolean }
    | { seconds_done: number; seconds_total: number }
    | null;
  /** Картинок в документе больше предела — обработаны первые по порядку. */
  images_truncated: boolean;
  /** Код причины, по которой не удалась последняя переобработка готового документа; иначе `null`. */
  reprocess_error: string | null;
  /** Готовый документ переобрабатывается по запросу пользователя. */
  reprocessing: boolean;
  /** Право удалить, отправить на повторную обработку и сменить теги. */
  can_delete: boolean;
}

/** Текст страницы с цитатой отрезками (контракт §8.3). */
export interface DocumentText {
  page: number | null;
  page_count: number | null;
  recognized: boolean;
  /** `start_seconds` — начало отрезка речи у записи; у документов и у продолжения отрезка — `null`. */
  segments: { text: string; highlight: boolean; start_seconds: number | null }[];
  /** У записи — с какой секунды начать проигрывание; у документов — `null`. */
  timecode_seconds: number | null;
  /** Картинка источника и её описание — только на странице этой картинки. */
  image: { id: string; description: string } | null;
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
