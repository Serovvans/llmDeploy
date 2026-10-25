/**
 * Чистые правила раздела «Код» (концепция §5.13, контракт §7.5.3): проверки имён и путей,
 * подстановки мастера, состояние строки «Путь до CoGIS», числа строк в разнице.
 */
import type {
  CodeBuild,
  CodeConfig,
  CodeFile,
  CodeProject,
  CodeSdk,
  ImportReport,
  WizardForm,
  WizardParameter,
  WizardTool,
} from '../api/types';
import { formatDate, formatSize, texts } from '../texts';

const w = texts.workshop;

/** Длина имени проекта — постоянная контракта (§7.5.3). */
export const PROJECT_NAME_MAX = 64;
const NAME_CHARS = /^[A-Za-z][A-Za-z0-9_]*$/;
const PARAM_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const PATH_PART = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
const PATH_MAX = 200;
const PATH_PARTS_MAX = 8;
const WINDOWS_RESERVED = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i;
/** Параметр подключения к базе, который каркас добавляет сам (контракт §7.5.4). */
export const DATABASE_PARAM = 'TargetWorkspace';

/** Ошибка имени проекта до отправки (концепция §5.13.4); служебные слова проверяет сервер. */
export function projectNameError(name: string): string | null {
  if (!name) {
    return w.name.enter;
  }
  if (!NAME_CHARS.test(name)) {
    return w.name.format;
  }
  return name.length > PROJECT_NAME_MAX ? w.name.tooLong : null;
}

/** Имя подходит под правило: в окне ZIP оно подставляется из имени архива. */
export function nameFromArchive(fileName: string): string {
  const base = fileName.replace(/\.zip$/i, '');
  return projectNameError(base) === null ? base : '';
}

/** Текст отказа сервера по полю «Имя проекта» — одинаковый в мастере, окнах примера, ZIP и переноса. */
export function projectNameServerError(code: string, name: string): string | null {
  switch (code) {
    case 'required':
      return w.name.enter;
    case 'too_long':
      return w.name.tooLong;
    case 'invalid_format':
      // Имя прошло правило символов — значит, оно занято: служебное слово C#, имя каркаса, имя библиотеки или имя Windows (контракт §7.5.3).
      return projectNameError(name) ?? w.name.reserved(name);
    default:
      return null;
  }
}

export type PathProblem = 'outside_project' | 'invalid_format' | 'extension_not_allowed';

/** Нарушение пути по правилам контракта §7.5.3 в его порядке; `null` — путь допустим. */
export function pathProblem(path: string, extensions: readonly string[]): PathProblem | null {
  const parts = path.split('/');
  if (path.startsWith('/') || /^[A-Za-z]:/.test(path) || path.includes('\\') || parts.some((part) => part === '.' || part === '..')) {
    return 'outside_project';
  }
  const partBad = (part: string) => !PATH_PART.test(part) || part.endsWith('.') || WINDOWS_RESERVED.test(part.split('.')[0] ?? '');
  if (path.length > PATH_MAX || parts.length > PATH_PARTS_MAX || parts.some(partBad)) {
    return 'invalid_format';
  }
  return extensions.some((extension) => path.endsWith(extension)) ? null : 'extension_not_allowed';
}

/** Текст недопустимого имени: один код на несколько причин, причина — по таблице §5.13.5, первая подошедшая. */
export function invalidPathText(path: string): string {
  const parts = path.split('/');
  if (parts.some((part) => part === '')) {
    return w.files.emptyPart;
  }
  if (parts.some((part) => part.endsWith('.'))) {
    return w.files.trailingDot;
  }
  const reserved = parts.find((part) => WINDOWS_RESERVED.test(part.split('.')[0] ?? ''));
  return reserved ? w.files.reserved(reserved) : w.files.invalid;
}

const lower = (value: string) => value.toLowerCase();

/**
 * Ошибка пути нового или переименованного файла (концепция §5.13.5): правила пути, повтор без учёта
 * регистра и совпадение с папкой (`path_conflict`). `self` — переименовываемый файл.
 */
export function filePathError(path: string, files: readonly CodeFile[], config: CodeConfig, self?: string): string | null {
  if (!path) {
    return w.files.enter;
  }
  switch (pathProblem(path, config.file_extensions)) {
    case 'outside_project':
      return w.files.outside;
    case 'invalid_format':
      return invalidPathText(path);
    case 'extension_not_allowed':
      return w.files.onlyCs;
  }
  const others = files.filter((file) => file.id !== self).map((file) => lower(file.path));
  const target = lower(path);
  if (others.includes(target)) {
    return w.files.exists;
  }
  if (others.some((other) => other.startsWith(`${target}/`) || target.startsWith(`${other}/`))) {
    return w.files.conflict;
  }
  if (self === undefined && files.length >= config.max_files) {
    return w.files.limit(config.max_files);
  }
  return null;
}

/** Текст отказа сервера по пути файла (таблица §5.13.15); `null` — код не про путь. */
export function filePathServerError(error: { code: string; fields: { field: string; code: string }[]; details: Record<string, unknown> }, path: string): string | null {
  if (error.code === 'file_exists') {
    return w.files.exists;
  }
  if (error.code === 'project_limit') {
    const max = Number(error.details.max);
    return error.details.limit === 'files' ? w.files.limit(max) : w.editor.projectBytes(formatSize(max));
  }
  const field = error.fields.find((item) => item.field === 'path');
  switch (field?.code) {
    case 'required':
      return w.files.enter;
    case 'extension_not_allowed':
      return w.files.onlyCs;
    case 'outside_project':
      return w.files.outside;
    case 'path_conflict':
      return w.files.conflict;
    case 'invalid_format':
      return invalidPathText(path);
    default:
      return null;
  }
}

/** Имя файла и папка — для вкладок и дерева. */
export function splitPath(path: string): { name: string; folder: string } {
  const at = path.lastIndexOf('/');
  return at < 0 ? { name: path, folder: '' } : { name: path.slice(at + 1), folder: path.slice(0, at) };
}

/** Подпись вкладки: при совпадении имён у открытых — с папкой серым («Helpers.cs · Geo», §5.13.6). */
export function tabLabel(path: string, openPaths: readonly string[]): { name: string; folder: string | null } {
  const { name, folder } = splitPath(path);
  const twins = openPaths.filter((other) => splitPath(other).name.toLowerCase() === name.toLowerCase()).length;
  return { name, folder: twins > 1 && folder ? folder : null };
}

export interface TreeFolder {
  kind: 'folder';
  path: string;
  name: string;
  children: TreeNode[];
}

export interface TreeFile {
  kind: 'file';
  file: CodeFile;
  name: string;
}

export type TreeNode = TreeFolder | TreeFile;

/** Дерево по путям проекта: папки сверху, затем файлы, по имени без учёта регистра. */
export function buildTree(files: readonly CodeFile[]): TreeNode[] {
  const root: TreeFolder = { kind: 'folder', path: '', name: '', children: [] };
  for (const file of files) {
    const parts = file.path.split('/');
    let folder = root;
    parts.slice(0, -1).forEach((part, index) => {
      const path = parts.slice(0, index + 1).join('/');
      let next = folder.children.find(
        (node): node is TreeFolder => node.kind === 'folder' && node.path.toLowerCase() === path.toLowerCase(),
      );
      if (!next) {
        next = { kind: 'folder', path, name: part, children: [] };
        folder.children.push(next);
      }
      folder = next;
    });
    folder.children.push({ kind: 'file', file, name: parts.at(-1) ?? file.path });
  }
  const sort = (nodes: TreeNode[]): TreeNode[] =>
    nodes
      .map((node) => (node.kind === 'folder' ? { ...node, children: sort(node.children) } : node))
      .sort((a, b) => (a.kind === b.kind ? a.name.localeCompare(b.name, 'en', { sensitivity: 'base' }) : a.kind === 'folder' ? -1 : 1));
  return sort(root.children);
}

/** Имя параметра из подписи по-английски: «Organization Name» → `OrganizationName` (§5.13.4). */
export function paramNameFrom(label: string): string {
  const name = (label.match(/[A-Za-z0-9]+/g) ?? []).map((part) => part[0]?.toUpperCase() + part.slice(1)).join('');
  return /^[0-9]/.test(name) ? `_${name}` : name;
}

/** Параметр в форме мастера: имя подставляется из подписи, пока сотрудник не менял его сам. */
export interface DraftParam extends WizardParameter {
  key: number;
  nameTouched: boolean;
}

/** Инструмент в форме мастера: «Другой значок…» хранит класс отдельно. */
export interface DraftTool extends Omit<WizardTool, 'inputs' | 'outputs'> {
  key: number;
  customIcon: boolean;
  inputs: DraftParam[];
  outputs: DraftParam[];
}

export interface WizardDraft {
  name: string;
  tools: DraftTool[];
}

/** Значок по умолчанию — «Шестерёнка» (§5.13.4). */
export const DEFAULT_ICON = 'fa fa-cog';

let keys = 0;
export function nextKey(): number {
  keys += 1;
  return keys;
}

export function emptyTool(): DraftTool {
  return {
    key: nextKey(),
    title_ru: '',
    title_en: '',
    category: '',
    icon: DEFAULT_ICON,
    customIcon: false,
    uses_database: false,
    description: '',
    inputs: [],
    outputs: [],
  };
}

export function emptyParam(type: string, input: boolean): DraftParam {
  return { key: nextKey(), name: '', nameTouched: false, type, ...(input ? { required: true } : {}), title_ru: '', title_en: '' };
}

/** Форма мастера для сервера (контракт §7.5.4): подписи без пробелов по краям. */
export function wizardForm(draft: WizardDraft): WizardForm {
  const param = (item: DraftParam): WizardParameter => ({
    name: item.name,
    type: item.type,
    ...(item.required === undefined ? {} : { required: item.required }),
    title_ru: item.title_ru.trim(),
    title_en: item.title_en.trim(),
  });
  return {
    name: draft.name,
    tools: draft.tools.map((tool) => ({
      title_ru: tool.title_ru.trim(),
      title_en: tool.title_en.trim(),
      category: tool.category.trim(),
      icon: tool.icon.trim(),
      uses_database: tool.uses_database,
      description: tool.description,
      inputs: tool.inputs.map(param),
      outputs: tool.outputs.map(param),
    })),
  };
}

/** Ошибки полей мастера по имени поля контракта (`tools.0.inputs.1.name`); пусто — форма верна. */
export function wizardErrors(draft: WizardDraft, maxDescription: number): Record<string, string> {
  const errors: Record<string, string> = {};
  const name = projectNameError(draft.name);
  if (name) {
    errors.name = name;
  }
  const e = w.wizard.errors;
  draft.tools.forEach((tool, t) => {
    const at = `tools.${t}`;
    if (!tool.title_ru.trim()) errors[`${at}.title_ru`] = e.titleRu;
    if (!tool.title_en.trim()) errors[`${at}.title_en`] = e.titleEn;
    if (!tool.category.trim()) errors[`${at}.category`] = e.category;
    if (tool.customIcon && !tool.icon.trim()) errors[`${at}.icon`] = e.iconClass;
    if (tool.description.length > maxDescription) errors[`${at}.description`] = e.description(maxDescription);
    const seen = new Set<string>();
    const check = (group: 'inputs' | 'outputs') =>
      tool[group].forEach((param, p) => {
        const field = `${at}.${group}.${p}`;
        const key = param.name.toLowerCase();
        if (!param.name) {
          errors[`${field}.name`] = e.paramName;
        } else if (!PARAM_NAME.test(param.name)) {
          errors[`${field}.name`] = e.paramFormat;
        } else if (seen.has(key)) {
          errors[`${field}.name`] = e.paramRepeat;
        } else if (tool.uses_database && key === DATABASE_PARAM.toLowerCase()) {
          errors[`${field}.name`] = e.paramDatabase;
        }
        seen.add(key);
        if (!param.title_ru.trim()) errors[`${field}.title_ru`] = e.labelRu;
        if (!param.title_en.trim()) errors[`${field}.title_en`] = e.labelEn;
      });
    check('inputs');
    check('outputs');
  });
  return errors;
}

/** Значение поля формы по имени поля контракта — для текста отказа сервера. */
function fieldValue(draft: WizardDraft, field: string): string {
  const [, t, group, p, key] = field.split('.');
  const tool = draft.tools[Number(t)];
  const param = group === 'inputs' || group === 'outputs' ? tool?.[group][Number(p)] : undefined;
  return key === 'name' ? (param?.name ?? '') : '';
}

/**
 * Отказ сервера по полю мастера — текстом концепции; `null` — для поля текста нет (показывается
 * `message` сервера). Имя поля — с префиксом `wizard.` или без: тело запроса вкладывает форму в `wizard`.
 */
export function wizardServerError(raw: string, code: string, draft: WizardDraft, maxDescription: number): { field: string; text: string | null } {
  const field = raw.replace(/^wizard\./, '');
  const e = w.wizard.errors;
  if (field === 'name') {
    return { field, text: projectNameServerError(code, draft.name) };
  }
  const leaf = field.split('.').at(-1);
  const isParam = /^tools\.\d+\.(inputs|outputs)\.\d+\./.test(field);
  if (isParam && leaf === 'name') {
    const name = fieldValue(draft, field);
    const tool = draft.tools[Number(field.split('.')[1])];
    if (code === 'invalid_format') {
      return {
        field,
        text: tool?.uses_database && name.toLowerCase() === DATABASE_PARAM.toLowerCase() ? e.paramDatabase : e.paramKeyword(name),
      };
    }
    return { field, text: code === 'required' ? e.paramName : null };
  }
  if (code === 'required') {
    const required: Record<string, string> = {
      title_ru: isParam ? e.labelRu : e.titleRu,
      title_en: isParam ? e.labelEn : e.titleEn,
      category: e.category,
      icon: e.iconClass,
    };
    return { field, text: (leaf && required[leaf]) ?? null };
  }
  if (leaf === 'description' && code === 'too_long') {
    return { field, text: e.description(maxDescription) };
  }
  return { field, text: null };
}

/** Число добавленных и убранных строк по сравнению с текущим файлом (концепция §5.13.11, Д-47). */
export function lineDiffStats(before: string, after: string): { added: number; removed: number } {
  const split = (text: string) => {
    if (text === '') {
      return [];
    }
    const lines = text.split(/\r\n|\r|\n/);
    if (lines.at(-1) === '') {
      lines.pop();
    }
    return lines;
  };
  let a = split(before);
  let b = split(after);
  // Общие начало и конец не влияют на результат — длинные файлы с короткой правкой считаются быстро.
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) {
    start += 1;
  }
  let end = 0;
  while (end < a.length - start && end < b.length - start && a[a.length - 1 - end] === b[b.length - 1 - end]) {
    end += 1;
  }
  a = a.slice(start, a.length - end);
  b = b.slice(start, b.length - end);
  // Длина наибольшей общей подпоследовательности строк — одной строкой таблицы.
  const row = new Uint32Array(b.length + 1);
  for (const line of a) {
    let diagonal = 0;
    for (let j = 1; j <= b.length; j += 1) {
      const above = row[j] ?? 0;
      row[j] = line === b[j - 1] ? diagonal + 1 : Math.max(above, row[j - 1] ?? 0);
      diagonal = above;
    }
  }
  const common = row[b.length] ?? 0;
  return { added: b.length - common, removed: a.length - common };
}

/** Итог строки «Путь до CoGIS» (таблица §5.13.8). */
export type RouteState =
  | { kind: 'disabled' }
  | { kind: 'building' }
  | { kind: 'unavailable' }
  | { kind: 'none' }
  | { kind: 'outdated'; build: CodeBuild }
  | { kind: 'timeout' }
  | { kind: 'failed'; errors: number }
  | { kind: 'findings'; findings: number }
  | { kind: 'ready'; build: CodeBuild };

export interface RouteInput {
  buildEnabled: boolean;
  build: CodeBuild | null;
  /** Ответ на «Собрать» в этой вкладке ещё не пришёл. */
  pending: boolean;
  /** `build_running` проекта, кроме сборки правки, которую помощник ведёт в этой вкладке. */
  running: boolean;
  /** Последний ответ — `503 build_unavailable`; держится до следующей сборки или перезагрузки. */
  unavailable: boolean;
  /** Файлы или пакеты изменились здесь после сборки: сервер ответил бы так же (контракт §7.5.3). */
  changedLocally: boolean;
}

export function routeState(input: RouteInput): RouteState {
  const { build } = input;
  if (!input.buildEnabled) {
    return { kind: 'disabled' };
  }
  if (input.pending || input.running) {
    return { kind: 'building' };
  }
  if (input.unavailable) {
    return { kind: 'unavailable' };
  }
  if (!build) {
    return { kind: 'none' };
  }
  if (build.outdated || input.changedLocally) {
    return { kind: 'outdated', build };
  }
  if (build.status === 'timeout') {
    return { kind: 'timeout' };
  }
  if (build.status === 'failed') {
    return { kind: 'failed', errors: build.diagnostics.filter((item) => item.severity === 'error').length };
  }
  const findings = build.findings?.length ?? 0;
  return findings > 0 ? { kind: 'findings', findings } : { kind: 'ready', build };
}

/** Ровно одна основная кнопка — следующий шаг (§5.13.8). */
export function primaryAction(state: RouteState): 'build' | 'fix' | 'download' | null {
  switch (state.kind) {
    case 'disabled':
      return null;
    case 'failed':
    case 'findings':
      return 'fix';
    case 'ready':
      return 'download';
    default:
      return 'build';
  }
}

/** Объявление смены итога словами (строка `role="status"`, §5.13.8); `''` — объявлять нечего. */
export function routeAnnouncement(state: RouteState): string {
  const a = w.build.announce;
  switch (state.kind) {
    case 'ready':
      return a.ok;
    case 'failed':
      return a.failed(state.errors);
    case 'findings':
      return a.findings(state.findings);
    case 'timeout':
      return a.timeout;
    case 'unavailable':
      return a.unavailable;
    case 'building':
      return a.building;
    default:
      return '';
  }
}

/** Значок итога в строке списка проектов (§5.13.3); `null` — без значка. */
export function listBadge(build: { status: string; outdated: boolean; findings: number | null } | null): 'ok' | 'failed' | 'findings' | null {
  if (!build) {
    return null;
  }
  if (build.status !== 'succeeded') {
    return 'failed';
  }
  return build.findings ? 'findings' : 'ok';
}

/** Строки заметки после загрузки ZIP (§5.13.3). */
export interface ImportLines {
  lines: string[];
  skipped: string[];
  more: number;
}

const LIST_LIMIT = 5;

function importReason(reason: string, path: string, config: CodeConfig): string {
  const r = w.imported.reasons;
  switch (reason) {
    case 'invalid_path':
      return r.invalid_path;
    case 'file_too_large':
      return r.file_too_large(formatSize(config.file_max_bytes));
    case 'not_text':
      return r.not_text;
    case 'duplicate':
      return r.duplicate;
    case 'project_limit':
      return r.project_limit(config.max_files, formatSize(config.project_max_bytes));
    case 'unreadable':
      return path.toLowerCase().endsWith('.csproj') ? r.unreadableCsproj : r.unreadable;
    default:
      return r.other;
  }
}

export function importLines(report: ImportReport, project: CodeProject, config: CodeConfig, sdk: CodeSdk | null): ImportLines {
  const t = w.imported;
  const lines = [t.files(project.files.length)];
  const replaced = report.unmatched_packages.filter((item) => item.offline_version !== null);
  const missing = report.unmatched_packages.filter((item) => item.offline_version === null);
  const version = (id: string) => sdk?.packages.find((item) => item.id === id)?.version;
  const matched = project.packages.filter((id) => !replaced.some((item) => item.id.toLowerCase() === id.toLowerCase()));
  if (matched.length > 0) {
    lines.push(t.matched(matched.map((id) => [id, version(id)].filter(Boolean).join(' ')).join(', ')));
  }
  if (replaced.length > 0) {
    lines.push(t.replaced(replaced.map((item) => t.replacedItem(item.id, item.offline_version ?? '', item.version)).join(', ')));
  }
  if (missing.length > 0) {
    lines.push(t.missing(missing.map((item) => [item.id, item.version].filter(Boolean).join(' ')).join(', ')));
  }
  const skipped = report.skipped_files.slice(0, LIST_LIMIT).map((item) => t.skippedLine(item.path, importReason(item.reason, item.path, config)));
  if (report.skipped_total > 0) {
    lines.push(t.skipped(report.skipped_total));
  }
  const tail = report.ignored_files > 0 ? [t.ignored(report.ignored_files)] : [];
  return { lines: [...lines, ...tail], skipped, more: Math.max(0, report.skipped_total - skipped.length) };
}

/** Причина отказа портала у файла правки (таблица §5.13.11); `null` — строка без причины. */
export function rejectedReason(reason: string, config: CodeConfig): string | null {
  const r = w.edit.reasons;
  switch (reason) {
    case 'outside_project':
    case 'invalid_path':
    case 'extension_not_allowed':
    case 'path_conflict':
    case 'empty_file':
      return r[reason];
    case 'file_too_large':
      return r.file_too_large(formatSize(config.file_max_bytes));
    case 'project_limit':
      return r.project_limit(config.max_files, formatSize(config.project_max_bytes));
    default:
      return null;
  }
}

/** Время со временем суток и для давних дат: у версий одного дня оно различает строки (§5.13.13). */
export function formatDateTime(iso: string, now: Date = new Date()): string {
  const words = formatDate(iso, now);
  if (words.includes(',')) {
    return words;
  }
  const date = new Date(iso);
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${words}, ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** В ответе идёт блок правки (`csharp path=…`, контракт §7.5.10): строка состояния — «Готовлю правку…». */
export function hasEditBlock(text: string): boolean {
  return /^[ \t]*(`{3,}|~{3,})[ \t]*(csharp|cs|c#)(?![\w#])[^\n]*\bpath=/im.test(text);
}
