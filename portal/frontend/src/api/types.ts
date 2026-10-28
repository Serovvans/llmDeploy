/** Типы контракта `docs/portal-api.md` версии 0.23 (поля релизов 0.7–1.0). Имена полей — как в контракте. */

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
  /** Раздел «Код» (контракт §4, §7.5): флаги раздела и сборки, пределы и типы параметров мастера. */
  code: CodeConfig;
  /** Подключения к базам заказчика (контракт §4, §7.6; 1.0). */
  db: DbConfig;
}

export interface DbConfig {
  enabled: boolean;
  /** Корневой сертификат заказчика задан и читается: доступен режим «С проверкой сертификата». */
  tls_verify_available: boolean;
  sample_rows_check: number;
  sample_rows_preview: number;
  max_selected_tables: number;
}

export interface CodeParameterType {
  id: string;
  title: string;
  csharp: string;
  input: boolean;
  output: boolean;
}

export interface CodeConfig {
  enabled: boolean;
  build_enabled: boolean;
  file_extensions: string[];
  max_projects: number;
  max_files: number;
  file_max_bytes: number;
  project_max_bytes: number;
  import_max_bytes: number;
  max_snapshots: number;
  fix_attempts: number;
  wizard: { max_tools: number; max_parameters: number; parameter_types: CodeParameterType[] };
  /** Виды проектов (контракт §7.5.16; 1.0). */
  kinds: { id: CodeKind; extensions: string[]; single_file: boolean }[];
  /** Флаг предпросмотра и пределы журнала и ошибок (контракт §7.5.17; 1.0). */
  preview: { enabled: boolean; log_max_entries: number; errors_max: number };
}

/** Вид проекта «Код» (контракт §7.5.16); всё, кроме `gptool`, — веб-вид. */
export type CodeKind = 'gptool' | 'js_plugin' | 'html_widget' | 'catalog' | 'card_hooks';

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

export type DialogKind = 'chat' | 'sql' | 'cogis' | 'docparse' | 'code';

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
  /** Только у `sql` в `GET /api/dialogs/{id}` и ответе `PATCH`: выбор подключения (контракт §7.6.9; 1.0). */
  db?: DbSelection | null;
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
  /** Только у источника `code`: проект, на который ведёт ссылка (контракт §5.1, 0.9). */
  source_project_id?: string;
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
  /** (1.0) `null` у сохранённого блока проверки на образце: строки базы не хранятся (контракт §7.4). */
  rows: (string | null)[][] | null;
  /** Строк было больше, чем отдано. */
  truncated: boolean;
}

/** Таблица тестовых данных блока (контракт §7.4); `origin` и `invented_reason` — с 1.0. */
export interface SqlRunFixture extends SqlRunRows {
  table: string;
  origin?: 'sample' | 'invented';
  invented_reason?: 'not_in_snapshot' | 'excluded' | 'samples_not_allowed' | 'table_limit' | null;
}

/** Проверка на образце (контракт §7.4, 1.0): `null` — без подключения. */
export interface SqlRunDb {
  connection_title: string;
  sample_rows: number;
  excluded_columns: { table: string; column: string; reason: 'excluded' | 'binary' }[];
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
  /** (1.0) Подключение проверки; `null` или нет поля — проверка без подключения. */
  db?: SqlRunDb | null;
  fixture: SqlRunFixture[] | null;
  /** `row_count` — с 1.0: сколько строк прочитано. */
  result: (SqlRunRows & { row_count?: number }) | null;
  affected_rows: number | null;
  /** Сообщение СУБД: при `failed` — последняя ошибка, при `fixed` — ошибка исходного запроса. */
  error: string | null;
  /** (1.0) Что в `error`: текст или код SQLSTATE с именем условия (только у сохранённой проверки на образце). */
  error_kind?: 'message' | 'sqlstate' | null;
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
  /** Только у вопроса в диалоге `code`: фрагмент, с которым задан вопрос (путь — на момент вопроса). */
  code_target?: CodeTarget | null;
  /** Только у ответа в диалоге `code`: правка помощника. */
  code_edit?: CodeEdit | null;
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
/** Действие вопроса помощнику проекта (контракт §7.5.8): `fix` — только кнопкой «Исправить с помощником». */
export type CodeAction = 'write' | 'explain' | 'fix';

/** Параметры вопроса по виду диалога (контракт §5.3); к ним добавляется `content`. */
export type QuestionParams =
  | { mode: AnswerMode; collections: KbScope[]; tags?: string[] }
  | { action: SqlAction; dialect: string; schema_id: string | null; test_run: boolean }
  | { action: CogisAction }
  | {
      action: CodeAction;
      /** `null` — все файлы проекта; список — только эти (открытые вкладки). */
      file_ids: string[] | null;
      target: { file_id: string; start_line: number; end_line: number } | null;
    }
  | Record<string, never>;

/** Тип ошибки предпросмотра — закрытый список (контракт §7.5.16, 0.25). */
export const PREVIEW_ERROR_TYPES = [
  'Error',
  'AggregateError',
  'EvalError',
  'InternalError',
  'RangeError',
  'ReferenceError',
  'SyntaxError',
  'TypeError',
  'URIError',
  'DOMException',
] as const;
export type PreviewErrorType = (typeof PREVIEW_ERROR_TYPES)[number];

/**
 * Запись `preview_fix.problems` (контракт §7.5.16, 0.25): `text` — только при `layer: "invented"`.
 * `path` — путь файла проекта или `null`; при `path: null` и `line` — `null`.
 */
export type PreviewFixProblem =
  | { kind: 'error'; path: string | null; line: number | null; type: PreviewErrorType; text?: string }
  | { kind: 'network'; path: null; line: null; directive: string; text?: string };

/** «Исправить с помощником» по ошибкам предпросмотра: текст вопроса собирает сервер (контракт §7.5.16, 0.25). */
export interface PreviewFix {
  layer: 'invented' | 'sample';
  problems: PreviewFixProblem[];
}

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

/* ---------- Раздел «Код» (контракт §7.5, 0.9) ---------- */

export type CatalogInsertion = 'head_start' | 'head_end' | 'body_start' | 'body_end';
export type CatalogClient = 'smartphone' | 'tablet' | 'desktop';

/** Как вставлять файл в Каталог (контракт §7.5.16): только у проекта `catalog`. */
export interface CatalogPlacement {
  insertion: CatalogInsertion;
  clients: CatalogClient[];
}

export interface CodeFile {
  id: string;
  path: string;
  content: string;
  /** Номер изменения файла, с 1. */
  revision: number;
  updated_at: string;
  /** (1.0) Только у проекта `catalog`; у остальных — `null` или нет поля. */
  catalog?: CatalogPlacement | null;
}

export type BuildStatus = 'succeeded' | 'failed' | 'timeout';

/** Сообщение сборки: строки и столбцы — с 1; `file` — `null` у сообщения без места. */
export interface Diagnostic {
  severity: 'error' | 'warning';
  code: string;
  file: string | null;
  line: number | null;
  column: number | null;
  end_line: number | null;
  end_column: number | null;
  message: string;
}

/** Находка правил GPTool (контракт §7.5.13): место — `null` у находки без места. */
export interface RuleFinding {
  rule: string;
  file: string | null;
  line: number | null;
  column: number | null;
  message: string;
}

export interface CodeArchive {
  files: { file_name: string; kind: 'tool' | 'package' | 'memo' }[];
  on_server: string[];
}

export interface CodeBuild {
  status: BuildStatus;
  /** Файлы или пакеты проекта изменились после этой сборки. */
  outdated: boolean;
  sdk_version: string | null;
  packages: { id: string; version: string }[];
  diagnostics: Diagnostic[];
  diagnostics_truncated: boolean;
  factory: string | null;
  tools: { name: string; title: string; category: string | null }[] | null;
  /** `[]` — нарушений нет; `null` — правила не проверялись. */
  findings: RuleFinding[] | null;
  archive: CodeArchive | null;
  created_at: string;
}

export interface CodeProject {
  id: string;
  name: string;
  packages: string[];
  schema_id: string | null;
  dialog_id: string;
  files: CodeFile[];
  /** Последняя сборка кнопкой «Собрать»; `null` — проект так не собирался. */
  build: CodeBuild | null;
  /** Идёт любая сборка проекта — кнопкой или проверка правки циклом исправления. */
  build_running: boolean;
  /** (1.0) Вид проекта; у прежних ответов поля нет — это `gptool`. */
  kind?: CodeKind;
  /** (1.0) Подключение и таблицы; `null` — не выбрано или подключения выключены. */
  db?: DbSelection | null;
  created_at: string;
  updated_at: string;
}

export interface CodeProjectItem {
  id: string;
  name: string;
  updated_at: string;
  build: { status: BuildStatus; outdated: boolean; findings: number | null } | null;
  /** (1.0) Вид проекта. */
  kind?: CodeKind;
}

export interface CodeTarget {
  file_id: string;
  path: string;
  start_line: number;
  end_line: number;
}

export type CodeRejectReason =
  | 'outside_project'
  | 'invalid_path'
  | 'extension_not_allowed'
  | 'file_too_large'
  | 'project_limit'
  | 'path_conflict'
  | 'empty_file';

export interface CodeVerification {
  status: 'succeeded' | 'failed' | 'timeout' | 'unavailable';
  builds: number;
  warnings: number;
  diagnostics: Diagnostic[];
  findings: RuleFinding[];
}

/** Правка помощника — `Message.code_edit` и событие `code_edit` (контракт §7.5.1). */
export interface CodeEdit {
  status: 'proposed' | 'accepted' | 'rejected' | 'superseded';
  files: { path: string; change: 'modified' | 'added'; decision: 'accepted' | 'rejected' | null }[];
  /** `path` — недоверенный текст из ответа модели. */
  rejected_files: { path: string; reason: string }[];
  incomplete: boolean;
  verification: CodeVerification | null;
  decided_at: string | null;
}

/** Правка с содержимым — `GET …/edits/{message_id}`. */
export interface CodeEditFull extends Omit<CodeEdit, 'files'> {
  files: (CodeEdit['files'][number] & {
    after: string;
    current: { revision: number; content: string } | null;
    outdated: boolean;
  })[];
}

export type CodeSnapshotReason = 'edit_accepted' | 'build_succeeded' | 'restored' | 'before_restore';

export interface CodeSnapshot {
  id: string;
  reason: CodeSnapshotReason;
  created_at: string;
  accepted_files: number | null;
  restored_from: string | null;
}

export interface WizardParameter {
  name: string;
  type: string;
  required?: boolean;
  title_ru: string;
  title_en: string;
}

export interface WizardTool {
  title_ru: string;
  title_en: string;
  category: string;
  icon: string;
  uses_database: boolean;
  description: string;
  inputs: WizardParameter[];
  outputs: WizardParameter[];
}

export interface WizardForm {
  name: string;
  tools: WizardTool[];
}

export type ImportSkipReason = 'invalid_path' | 'file_too_large' | 'not_text' | 'duplicate' | 'project_limit' | 'unreadable';

export interface ImportReport {
  ignored_files: number;
  /** No more than 100 entries (contract §7.5.5, А-121): counts come from `skipped_total`. */
  skipped_files: { path: string; reason: string }[];
  skipped_total: number;
  unmatched_packages: { id: string; version: string | null; offline_version: string | null }[];
}

export interface CodeCreated {
  project: CodeProject;
  assistant_request: string | null;
  import: ImportReport | null;
}

export interface SdkMember {
  kind: 'constructor' | 'method' | 'property' | 'field' | 'event' | 'enum_value';
  name: string;
  signature: string;
}

export interface SdkType {
  namespace: string;
  name: string;
  kind: 'class' | 'struct' | 'interface' | 'enum' | 'delegate';
  origin: 'cogis' | 'dotnet';
  signature: string;
  members: SdkMember[];
}

/** Справочник и набор пакетов — `GET /api/code/sdk` (контракт §7.5.14). */
export interface CodeSdk {
  available: boolean;
  sdk_version: string | null;
  libraries: { file_name: string; version: string | null }[];
  packages: { id: string; version: string; on_server: boolean }[];
  types: SdkType[];
}

/* ---------- Веб-виды и предпросмотр (контракт §7.5.16–7.5.18, 1.0) ---------- */

export type ClientLibraryId = 'jquery' | 'knockout' | 'leaflet';

/** Описание `api` и библиотеки клиента CoGIS — `GET /api/code/web`. */
export interface CodeWeb {
  /** `api.d.ts` целиком: функции `api` с описаниями JSDoc. */
  api_types: string;
  api_functions: string[];
  libraries: { id: ClientLibraryId; version: string | null; available: boolean }[];
}

export type ClientLibraryFile = 'jquery.js' | 'knockout.js' | 'leaflet.js' | 'leaflet.css';

/** Слой предпросмотра из образца — `GET …/preview-layer`. Строки — только в памяти вкладки. */
export interface PreviewLayerSample {
  connection_title: string;
  schema: string;
  table: string;
  rows: number;
  truncated: boolean;
  excluded_columns: number;
  geometry: { column: string; type: string } | null;
  features: GeoJsonFeatureCollection;
}

export interface GeoJsonFeatureCollection {
  type: 'FeatureCollection';
  features: { type: 'Feature'; geometry: { type: string; coordinates: unknown } | null; properties: Record<string, string | null> }[];
}

/* ---------- Подключения к базам заказчика (контракт §7.6, 1.0) ---------- */

/** Выбор подключения у диалога SQL и проекта (контракт §7.6.1). */
export interface DbSelection {
  /** `null` — подключение удалено. */
  connection_id: string | null;
  /** Название на момент выбора. */
  title: string;
  tables: DbTableRef[];
  /** Подключение есть, доступно сотруднику и подключения включены. */
  available: boolean;
}

export interface DbTableRef {
  schema: string;
  name: string;
}

export interface DbSnapshotSummary {
  taken_at: string;
  tables: number;
  truncated: boolean;
}

/** Строка списка сотрудника — `GET /api/db/connections`. */
export interface DbConnection {
  id: string;
  title: string;
  description: string;
  /** `false` — у учётной записи лишние права: база не читается, схема остаётся. */
  usable: boolean;
  samples_allowed: boolean;
  snapshot: DbSnapshotSummary | null;
  refresh_available_at: string | null;
}

export interface DbColumn {
  name: string;
  type: string;
  not_null: boolean;
  primary_key: boolean;
  references: { schema: string; table: string; column: string } | null;
  comment: string | null;
  geometry: { type: string; srid: number } | null;
  excluded: boolean;
}

export interface DbTable {
  schema: string;
  name: string;
  kind: 'table' | 'view';
  comment: string | null;
  samples_excluded: boolean;
  columns: DbColumn[];
}

/** Снимок схемы — `GET /api/db/connections/{id}/snapshot`. Имена и комментарии — недоверенный текст. */
export interface DbSnapshot {
  connection_id: string;
  taken_at: string;
  truncated: boolean;
  tables: DbTable[];
}

export type RightsKind =
  | 'superuser'
  | 'createrole'
  | 'createdb'
  | 'replication'
  | 'bypassrls'
  | 'owner'
  | 'write'
  | 'maintain'
  | 'create'
  | 'grant_option'
  | 'predefined_role'
  | 'admin_option'
  | 'server_settings'
  | 'catalog_function'
  | 'large_object'
  | 'lo_compat_privileges'
  | 'definer_function';

/** Лишнее право или предупреждение (контракт §7.6.1): `objects` — первые три, `total` — сколько всего. */
export interface RightsFinding {
  kind: RightsKind;
  privileges: string[];
  objects: { type: string; name: string }[];
  total: number;
}

export type DbTlsMode = 'verify' | 'require' | 'disable';

export type DbErrorReason =
  | 'unreachable'
  | 'timeout'
  | 'auth_failed'
  | 'database_not_found'
  | 'tls_failed'
  | 'tls_required'
  | 'busy'
  | 'schemas_unavailable'
  | 'host_forbidden'
  | 'other';

export interface SampleExclusion {
  schema: string;
  table: string;
  column: string | null;
}

/** Подключение глазами администратора — маршруты `/api/admin/db-connections`. Пароля нет ни в каком виде. */
export interface AdminDbConnection {
  id: string;
  title: string;
  description: string;
  host: string;
  port: number;
  database: string;
  username: string;
  tls_mode: DbTlsMode;
  /** (1.0.1) `true` — «все схемы», которые учётной записи разрешено читать; тогда `schemas` — `[]`. */
  all_schemas: boolean;
  schemas: string[];
  samples_allowed: boolean;
  sample_exclusions: SampleExclusion[];
  access: { all: boolean; users?: { id: string; login: string; full_name: string }[] };
  rights: {
    status: 'unknown' | 'read_only' | 'excess';
    checked_at: string | null;
    findings: RightsFinding[];
    warnings: RightsFinding[];
  };
  last_error: { reason: string; details: Record<string, unknown> | null; at: string } | null;
  snapshot: DbSnapshotSummary | null;
  refresh_available_at: string | null;
  created_at: string;
  updated_at: string;
}

/** Тело создания и изменения подключения; при изменении все поля необязательны. */
export interface DbConnectionInput {
  title: string;
  description: string;
  host: string;
  port: number;
  database: string;
  username: string;
  password?: string;
  tls_mode: DbTlsMode;
  /** (1.0.1) При `true` `schemas` не отправляется; переход к списку — `false` вместе со `schemas` (контракт §7.6.2). */
  all_schemas: boolean;
  schemas?: string[];
  samples_allowed: boolean;
  sample_exclusions: SampleExclusion[];
  access: { all: true } | { all: false; user_ids: string[] };
}
