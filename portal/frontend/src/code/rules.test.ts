import { describe, expect, it } from 'vitest';

import type { CodeBuild, CodeFile, CodeProject } from '../api/types';
import { CONFIG } from '../test/mockApi';
import { texts } from '../texts';
import {
  buildTree,
  emptyParam,
  emptyTool,
  filePathError,
  hasEditBlock,
  importLines,
  invalidPathText,
  lineDiffStats,
  paramNameFrom,
  pathProblem,
  primaryAction,
  projectNameError,
  projectNameServerError,
  routeState,
  wizardErrors,
  wizardServerError,
  type RouteInput,
  type WizardDraft,
} from './rules';

const w = texts.workshop;
const code = CONFIG.code;

function file(path: string, id = path): CodeFile {
  return { id, path, content: '', revision: 1, updated_at: '2026-10-24T09:00:00Z' };
}

function build(over: Partial<CodeBuild> = {}): CodeBuild {
  return {
    status: 'succeeded',
    outdated: false,
    sdk_version: 'stub',
    packages: [],
    diagnostics: [],
    diagnostics_truncated: false,
    factory: 'A.GpToolFactory',
    tools: [],
    findings: [],
    archive: { files: [], on_server: [] },
    created_at: '2026-10-24T09:00:00Z',
    ...over,
  };
}

describe('имя проекта', () => {
  it('проверяет символы и длину; служебные слова — сервер', () => {
    expect(projectNameError('')).toBe(w.name.enter);
    expect(projectNameError('1Tool')).toBe(w.name.format);
    expect(projectNameError('My Tool')).toBe(w.name.format);
    expect(projectNameError(`A${'b'.repeat(64)}`)).toBe(w.name.tooLong);
    expect(projectNameError('class')).toBeNull();
    expect(projectNameServerError('invalid_format', 'class')).toBe(w.name.reserved('class'));
    expect(projectNameServerError('invalid_format', 'My Tool')).toBe(w.name.format);
  });
});

describe('путь файла', () => {
  it('выход за пределы проекта — раньше прочих правил', () => {
    for (const path of ['/A.cs', 'C:/A.cs', 'a\\b.cs', '../A.cs', 'Geo/./A.cs']) {
      expect(pathProblem(path, ['.cs'])).toBe('outside_project');
    }
  });

  it('текст недопустимого имени выбирается по причине в порядке концепции', () => {
    expect(pathProblem('Geo//A.cs', ['.cs'])).toBe('invalid_format');
    expect(invalidPathText('Geo//A.cs')).toBe(w.files.emptyPart);
    expect(invalidPathText('Geo/')).toBe(w.files.emptyPart);
    expect(invalidPathText('Tools./A.cs')).toBe(w.files.trailingDot);
    expect(invalidPathText('Com1.Helper.cs')).toBe(w.files.reserved('Com1.Helper.cs'));
    expect(invalidPathText('aux/A.cs')).toBe(w.files.reserved('aux'));
    expect(invalidPathText('Мой файл.cs')).toBe(w.files.invalid);
    expect(pathProblem('a/b/c/d/e/f/g/h/i.cs', ['.cs'])).toBe('invalid_format');
    expect(pathProblem('Helpers.txt', ['.cs'])).toBe('extension_not_allowed');
    expect(pathProblem('Geo/Helpers.cs', ['.cs'])).toBeNull();
  });

  it('повтор и совпадение с папкой — без учёта регистра', () => {
    const files = [file('Geo/Helpers.cs', 'f1'), file('Main.cs', 'f2')];
    expect(filePathError('geo/helpers.CS'.replace('.CS', '.cs'), files, code)).toBe(w.files.exists);
    expect(filePathError('GEO', files, code)).toBe(w.files.onlyCs);
    expect(filePathError('geo.cs/A.cs', files, code)).toBeNull();
    expect(filePathError('Main.cs/A.cs', files, code)).toBe(w.files.conflict);
    expect(filePathError('Geo/Helpers.cs', files, code, 'f1')).toBeNull();
    expect(filePathError('New.cs', files, { ...code, max_files: 2 })).toBe(w.files.limit(2));
  });

  it('дерево: папки сверху, файлы по имени', () => {
    const tree = buildTree([file('b.cs'), file('Geo/z.cs'), file('a.cs'), file('geo/A.cs')]);
    expect(tree.map((node) => node.name)).toEqual(['Geo', 'a.cs', 'b.cs']);
    const folder = tree[0];
    expect(folder?.kind === 'folder' && folder.children.map((node) => node.name)).toEqual(['A.cs', 'z.cs']);
  });
});

describe('мастер', () => {
  function draft(): WizardDraft {
    const tool = { ...emptyTool(), title_ru: 'Добавить', title_en: 'Add', category: 'Утилиты' };
    tool.inputs = [
      { ...emptyParam('string', true), name: 'Email', title_ru: 'Email', title_en: 'Email' },
      { ...emptyParam('string', true), name: 'email', title_ru: 'Ещё', title_en: 'More' },
    ];
    return { name: 'AddressTool', tools: [tool] };
  }

  it('имя параметра — из подписи по-английски', () => {
    expect(paramNameFrom('Organization Name')).toBe('OrganizationName');
    expect(paramNameFrom('e-mail address')).toBe('EMailAddress');
    expect(paramNameFrom('2nd value')).toBe('_2ndValue');
  });

  it('повтор имени параметра — без учёта регистра, как на сервере', () => {
    expect(wizardErrors(draft(), 100)).toEqual({ 'tools.0.inputs.1.name': w.wizard.errors.paramRepeat });
  });

  it('отказ сервера по имени параметра: при базе данных — занято подключением, иначе служебное слово', () => {
    const form = draft();
    const first = form.tools[0];
    if (!first?.inputs[0]) throw new Error('form');
    first.inputs[0].name = 'TargetWorkspace';
    first.uses_database = true;
    expect(wizardServerError('wizard.tools.0.inputs.0.name', 'invalid_format', form, 100).text).toBe(w.wizard.errors.paramDatabase);
    first.uses_database = false;
    expect(wizardServerError('tools.0.inputs.0.name', 'invalid_format', form, 100)).toEqual({
      field: 'tools.0.inputs.0.name',
      text: w.wizard.errors.paramKeyword('TargetWorkspace'),
    });
    expect(wizardServerError('tools', 'too_long', form, 100).text).toBeNull();
  });
});

describe('разница строк', () => {
  it('считает добавленные и убранные строки по общей подпоследовательности', () => {
    expect(lineDiffStats('', 'a\nb\n')).toEqual({ added: 2, removed: 0 });
    expect(lineDiffStats('a\nb\nc', 'a\nB\nc\nd')).toEqual({ added: 2, removed: 1 });
    expect(lineDiffStats('a\r\nb', 'a\nb\n')).toEqual({ added: 0, removed: 0 });
    expect(lineDiffStats('x\ny\nz', 'z\nx\ny')).toEqual({ added: 1, removed: 1 });
  });
});

describe('путь до CoGIS', () => {
  const base: RouteInput = { buildEnabled: true, build: null, pending: false, running: false, unavailable: false, changedLocally: false };

  it('одна основная кнопка — следующий шаг', () => {
    expect(primaryAction(routeState(base))).toBe('build');
    expect(routeState({ ...base, buildEnabled: false }).kind).toBe('disabled');
    expect(primaryAction(routeState({ ...base, buildEnabled: false }))).toBeNull();
    const failed = build({
      status: 'failed',
      findings: null,
      archive: null,
      diagnostics: [
        { severity: 'error', code: 'CS1002', file: 'A.cs', line: 1, column: 1, end_line: 1, end_column: 2, message: 'x' },
        { severity: 'warning', code: 'CS0168', file: 'A.cs', line: 2, column: 1, end_line: 2, end_column: 2, message: 'y' },
      ],
    });
    expect(routeState({ ...base, build: failed })).toEqual({ kind: 'failed', errors: 1 });
    expect(primaryAction(routeState({ ...base, build: failed }))).toBe('fix');
    const findings = build({ findings: [{ rule: 'factory', file: null, line: null, column: null, message: 'm' }], archive: null });
    expect(routeState({ ...base, build: findings })).toEqual({ kind: 'findings', findings: 1 });
    expect(primaryAction(routeState({ ...base, build: build() }))).toBe('download');
  });

  it('изменённый код важнее итога, сборка и «недоступна» — важнее всего', () => {
    expect(routeState({ ...base, build: build({ outdated: true }) }).kind).toBe('outdated');
    expect(routeState({ ...base, build: build(), changedLocally: true }).kind).toBe('outdated');
    expect(routeState({ ...base, build: build(), unavailable: true }).kind).toBe('unavailable');
    expect(routeState({ ...base, build: build(), unavailable: true, pending: true }).kind).toBe('building');
    expect(routeState({ ...base, build: build({ status: 'timeout', findings: null, archive: null }) }).kind).toBe('timeout');
  });
});

describe('заметка после загрузки ZIP', () => {
  it('разделяет совпавшие, взятые в версии набора и отсутствующие пакеты', () => {
    const project = { files: [file('A.cs'), file('B.cs')], packages: ['Npgsql', 'Dapper'] } as CodeProject;
    const sdk = { packages: [{ id: 'Npgsql', version: '9.0.3', on_server: true }, { id: 'Dapper', version: '2.1.35', on_server: false }] };
    const report = {
      ignored_files: 12,
      skipped_files: [
        { path: 'Old.cs', reason: 'file_too_large' },
        { path: 'App.csproj', reason: 'unreadable' },
      ],
      unmatched_packages: [
        { id: 'Npgsql', version: '8.0.3', offline_version: '9.0.3' },
        { id: 'Serilog', version: null, offline_version: null },
      ],
    };
    const result = importLines(report, project, code, sdk as never);
    expect(result.lines).toEqual([
      w.imported.files(2),
      w.imported.matched('Dapper 2.1.35'),
      w.imported.replaced('Npgsql 9.0.3 вместо 8.0.3'),
      w.imported.missing('Serilog'),
      w.imported.skipped(2),
      w.imported.ignored(12),
    ]);
    expect(result.skipped).toEqual(['Old.cs — файл больше 16 КБ', 'App.csproj — не удалось прочитать — пакеты из него не взяты']);
  });
});

it('блок правки узнаётся по ограждению с путём', () => {
  expect(hasEditBlock('Вот правка:\n```csharp path=Geo/A.cs\nclass A {}')).toBe(true);
  expect(hasEditBlock('~~~~ C# path="A.cs"\n')).toBe(true);
  expect(hasEditBlock('```csharp\nvar a = 1;\n```')).toBe(false);
});
