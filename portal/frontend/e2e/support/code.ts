/** Раздел «Код» через API (docs/portal-api.md §7.5): проекты, файлы, сборка, помощник, правки. */
import { expect, type APIRequestContext, type APIResponse } from '@playwright/test';

import { unzip } from './files';
import { must } from './must';
import { parseStream, type StreamEvent } from './portal';

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

export interface CodeFile {
  id: string;
  path: string;
  content: string;
  revision: number;
}

export interface Diagnostic {
  severity: 'error' | 'warning';
  code: string;
  file: string | null;
  line: number | null;
  column: number | null;
  message: string;
}

export interface RuleFinding {
  rule: string;
  file: string | null;
  line: number | null;
  column: number | null;
  message: string;
}

export interface CodeBuild {
  status: 'succeeded' | 'failed' | 'timeout';
  outdated: boolean;
  sdk_version: string | null;
  packages: { id: string; version: string }[];
  diagnostics: Diagnostic[];
  factory: string | null;
  tools: { name: string; title: string; category: string | null }[] | null;
  findings: RuleFinding[] | null;
  archive: { files: { file_name: string; kind: 'tool' | 'package' | 'memo' }[]; on_server: string[] } | null;
}

export interface CodeProject {
  id: string;
  name: string;
  packages: string[];
  schema_id: string | null;
  dialog_id: string;
  files: CodeFile[];
  build: CodeBuild | null;
  build_running: boolean;
}

export interface CodeEdit {
  status: 'proposed' | 'accepted' | 'rejected' | 'superseded';
  files: { path: string; change: 'modified' | 'added'; decision: 'accepted' | 'rejected' | null }[];
  rejected_files: { path: string; reason: string }[];
  incomplete: boolean;
  verification: {
    status: 'succeeded' | 'failed' | 'timeout' | 'unavailable';
    builds: number;
    warnings: number;
    diagnostics: Diagnostic[];
    findings: RuleFinding[];
  } | null;
}

/** Инструмент формы мастера: без базы, без параметров, без описания — поля можно переопределить. */
export function wizardTool(overrides: Partial<WizardTool> = {}): WizardTool {
  return {
    title_ru: 'Проверка адреса',
    title_en: 'Address Check',
    category: 'Утилиты',
    icon: 'fa fa-map',
    uses_database: false,
    description: '',
    inputs: [],
    outputs: [],
    ...overrides,
  };
}

/** Строковый параметр мастера. */
export function stringParameter(name: string, required?: boolean): WizardParameter {
  return { name, type: 'string', ...(required === undefined ? {} : { required }), title_ru: `Поле ${name}`, title_en: `Field ${name}` };
}

export async function createWizardProject(
  api: APIRequestContext,
  name: string,
  tools: WizardTool[],
): Promise<{ project: CodeProject; assistant_request: string | null }> {
  const response = await api.post('/api/code/projects', { data: { source: 'wizard', wizard: { name, tools } } });
  expect(response.status(), await response.text()).toBe(201);
  return response.json();
}

export async function createExampleProject(api: APIRequestContext, name: string): Promise<CodeProject> {
  const response = await api.post('/api/code/projects', { data: { source: 'example', name } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).project;
}

/** Проект мастера из одного инструмента без базы и без параметров. */
export async function simpleProject(api: APIRequestContext, name: string): Promise<CodeProject> {
  return (await createWizardProject(api, name, [wizardTool()])).project;
}

export async function getProject(api: APIRequestContext, id: string): Promise<CodeProject> {
  const response = await api.get(`/api/code/projects/${id}`);
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

export function fileOf(project: CodeProject, path: string): CodeFile {
  return must(
    project.files.find((file) => file.path.toLowerCase() === path.toLowerCase()),
    `файл ${path} в проекте ${project.name}`,
  );
}

/** Нажатие «Собрать»: ответ сборки с проверкой статуса 200. */
export async function build(api: APIRequestContext, projectId: string): Promise<CodeBuild> {
  const response = await api.post(`/api/code/projects/${projectId}/build`, { timeout: 120_000 });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

/** Сборка без проверки статуса — для отказов. */
export function buildRaw(api: APIRequestContext, projectId: string): Promise<APIResponse> {
  return api.post(`/api/code/projects/${projectId}/build`, { timeout: 120_000 });
}

export async function addFile(api: APIRequestContext, projectId: string, path: string, content: string): Promise<CodeFile> {
  const response = await api.post(`/api/code/projects/${projectId}/files`, { data: { path, content } });
  expect(response.status(), await response.text()).toBe(201);
  return response.json();
}

/** Сохраняет новое содержимое файла по его текущей ревизии. */
export async function saveFile(api: APIRequestContext, projectId: string, file: CodeFile, content: string): Promise<CodeFile> {
  const response = await api.patch(`/api/code/projects/${projectId}/files/${file.id}`, {
    data: { revision: file.revision, content },
  });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

/** Вопрос помощнику проекта; возвращает события потока после его завершения. */
export async function askCode(
  api: APIRequestContext,
  dialogId: string,
  content: string,
  action: 'write' | 'explain' | 'fix' = 'write',
  options: { fileIds?: string[] | null; timeout?: number } = {},
): Promise<StreamEvent[]> {
  const response = await api.post(`/api/dialogs/${dialogId}/messages`, {
    data: { content, action, file_ids: options.fileIds ?? null, target: null },
    timeout: options.timeout ?? 60_000,
  });
  expect(response.status(), await response.text()).toBe(200);
  return parseStream(await response.text());
}

/** Правка из событий потока ответа. */
export function editOf(events: StreamEvent[]): CodeEdit {
  return must(events.find((event) => event.event === 'code_edit'), 'событие code_edit').data as CodeEdit;
}

/** Идентификатор последнего ответа диалога (у него правка). */
export async function lastAnswerId(api: APIRequestContext, dialogId: string): Promise<string> {
  const messages = await (await api.get(`/api/dialogs/${dialogId}/messages`)).json();
  const answers = (messages.items as { id: string; role: string }[]).filter((item) => item.role === 'assistant');
  return must(answers.at(-1), 'ответ помощника').id;
}

/** Блок правки «файл целиком» (§7.5.10): сервер сохраняет содержимое с переводом строки в конце. */
export function editBlock(path: string, content: string): string {
  return `\`\`\`csharp path=${path}\n${content}\n\`\`\``;
}

/** Скачивает ZIP и возвращает его файлы по именам. */
export async function downloadZip(api: APIRequestContext, path: string): Promise<{ response: APIResponse; files: Map<string, Buffer> }> {
  const response = await api.get(path);
  expect(response.status(), response.ok() ? '' : await response.text()).toBe(200);
  expect(response.headers()['content-type']).toContain('application/zip');
  return { response, files: unzip(await response.body()) };
}

/**
 * Файл, который не компилируется: в теле метода — необъявленное имя на строке 5. Пространство
 * имён — имя проекта, чтобы файл не мешал правилам GPTool своим расположением.
 */
export function brokenFile(namespace: string, marker = 'connStr'): string {
  return [
    `namespace ${namespace};`,
    '',
    'internal static class Broken',
    '{',
    `    internal static string Value() => ${marker};`,
    '}',
  ].join('\n');
}
