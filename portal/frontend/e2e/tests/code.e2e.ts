/**
 * Раздел «Код» 0.9 через API (docs/portal-design.md §10, критерии 33–37; docs/portal-api.md §7.5) на
 * стенде с настоящей песочницей code-sandbox и заглушкой SDK. Модель — заглушка (§12.2): правило 2а
 * отвечает правкой первого файла проекта, `[[stub:reply]]` — ровно заданным текстом,
 * `[[stub:code-fix]]` — ответом цикла исправления.
 */
import { uniqueSuffix } from '../support/accounts';
import {
  addFile,
  askCode,
  brokenFile,
  build,
  buildRaw,
  createExampleProject,
  createWizardProject,
  downloadZip,
  editBlock,
  editOf,
  fileOf,
  getProject,
  lastAnswerId,
  saveFile,
  simpleProject,
  stringParameter,
  wizardTool,
  type CodeBuild,
  type CodeProject,
  type WizardTool,
} from '../support/code';
import { zip } from '../support/files';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { answerText, eventsOf, expectError, saveSqlSchema, stubReply } from '../support/portal';
import { standCompose } from '../support/stand';

const CYRILLIC = /[а-яё]/i;

/** Номер строки (с 1), в которой впервые встречается `needle`. */
function lineOf(content: string, needle: string): number {
  const index = content.split('\n').findIndex((line) => line.includes(needle));
  expect(index, `строка «${needle}» в файле`).toBeGreaterThanOrEqual(0);
  return index + 1;
}

/** Сборка прошла, правила соблюдены, архив готов (критерий 33). */
function expectClean(result: CodeBuild, project: CodeProject, tools: number): void {
  expect(result.status).toBe('succeeded');
  expect(result.diagnostics.filter((item) => item.severity === 'error')).toEqual([]);
  expect(result.findings).toEqual([]);
  expect(result.factory).toBe(`${project.name}.GpToolFactory`);
  expect(result.tools).toHaveLength(tools);
  expect(must(result.archive, 'архив').files.map((file) => file.kind)).toContain('tool');
}

test.describe('Критерий 33: каркас мастера собирается без правок и проходит правила GPTool', () => {
  const cases: { title: string; tools: WizardTool[]; classes: string[] }[] = [
    { title: 'один инструмент без базы', tools: [wizardTool()], classes: ['AddressCheckTool'] },
    {
      title: 'один инструмент с базой, входным и выходным параметром',
      tools: [
        wizardTool({
          uses_database: true,
          inputs: [stringParameter('Email', true), { name: 'Count', type: 'int', required: false, title_ru: 'Число', title_en: 'Count' }],
          outputs: [stringParameter('Result')],
        }),
      ],
      classes: ['AddressCheckTool'],
    },
    {
      title: 'три инструмента, один из них с базой',
      tools: [
        wizardTool({ title_en: 'First Step', inputs: [stringParameter('Name', true)] }),
        wizardTool({ title_en: 'Second Step', uses_database: true, outputs: [stringParameter('Result')] }),
        wizardTool({ title_en: 'Third Step', inputs: [{ name: 'When', type: 'datetime', required: true, title_ru: 'Когда', title_en: 'When' }] }),
      ],
      classes: ['FirstStepTool', 'SecondStepTool', 'ThirdStepTool'],
    },
    { title: 'title_en «Gp» даёт класс GpTool2', tools: [wizardTool({ title_en: 'Gp' })], classes: ['GpTool2'] },
    {
      title: 'параметр WorkspaceName без базы',
      tools: [wizardTool({ inputs: [stringParameter('WorkspaceName', true)] })],
      classes: ['AddressCheckTool'],
    },
  ];

  for (const item of cases) {
    test(item.title, async ({ newMember }) => {
      const member = await newMember();
      const { project } = await createWizardProject(member.api, 'AddressTools', item.tools);
      expect(project.files.map((file) => file.path).sort()).toEqual(['GpToolFactory.cs', ...item.classes.map((name) => `${name}.cs`)].sort());
      if (item.tools.some((tool) => tool.uses_database)) {
        expect(project.packages).toContain('Npgsql');
      }
      const result = await build(member.api, project.id);
      expectClean(result, project, item.tools.length);
      expect(must(result.tools, 'инструменты').map((tool) => tool.name).sort()).toEqual(item.classes.map((name) => `AddressTools.${name}`).sort());
    });
  }

  test('проект из примера собирается и проходит правила; описание в мастере даёт запрос помощнику', async ({ newMember }) => {
    const member = await newMember();
    const example = await createExampleProject(member.api, 'ContactExample');
    expectClean(await build(member.api, example.id), example, 1);

    const created = await createWizardProject(member.api, 'Described', [wizardTool({ description: 'Проверяет адрес по справочнику.' })]);
    expect(created.assistant_request).toContain('Проверяет адрес по справочнику.');
  });
});

test.describe('Критерий 34: сборка', () => {
  test('ошибка компиляции — на русском, с файлом и строкой; без успешной сборки архива нет', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'BrokenBuild');
    await expectError(await member.api.get(`/api/code/projects/${project.id}/build/archive`), 409, 'archive_unavailable');

    await addFile(member.api, project.id, 'Broken.cs', brokenFile(project.name));
    const result = await build(member.api, project.id);
    expect(result.status).toBe('failed');
    const error = must(result.diagnostics.find((item) => item.severity === 'error'), 'ошибка компиляции');
    expect(error).toMatchObject({ code: 'CS0103', file: 'Broken.cs', line: 5 });
    expect(error.message).toMatch(CYRILLIC);
    expect(error.message).toContain('connStr');
    expect(result.findings).toBeNull();
    expect(result.archive).toBeNull();

    const archive = await member.api.get(`/api/code/projects/${project.id}/build/archive`);
    expect(archive.status()).toBe(409);
    expect((await archive.json()).error.details.reason).toBe('not_built');
  });

  test('архива нет и до первой сборки: причина no_build', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'NeverBuilt');
    const archive = await member.api.get(`/api/code/projects/${project.id}/build/archive`);
    expect(archive.status()).toBe(409);
    expect((await archive.json()).error.details.reason).toBe('no_build');
  });

  test('предупреждения сборке не мешают: собрано, архив готов', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'WithWarnings');
    await addFile(
      member.api,
      project.id,
      'Warn.cs',
      [`namespace ${project.name};`, '', 'internal static class Warn', '{', '#warning Проверка предупреждения', '    internal static int Value() { int unused = 0; return 1; }', '}'].join('\n'),
    );
    const result = await build(member.api, project.id);
    expect(result.status).toBe('succeeded');
    const warnings = result.diagnostics.filter((item) => item.severity === 'warning');
    expect(warnings.length).toBeGreaterThan(0);
    expect(warnings.every((item) => item.file === 'Warn.cs' && item.line !== null)).toBe(true);
    expect(result.findings).toEqual([]);
    expect(result.archive).not.toBeNull();
  });

  test('песочница не исполняет код проекта при сборке', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'NoExecution');
    const mark = `executed-${uniqueSuffix()}`;
    await addFile(
      member.api,
      project.id,
      'Init.cs',
      [
        'using System.Runtime.CompilerServices;',
        `namespace ${project.name};`,
        'internal static class Init',
        '{',
        '    [ModuleInitializer]',
        `    internal static void Run() => System.IO.File.WriteAllText("/tmp/${mark}", "x");`,
        '}',
      ].join('\n'),
    );
    expect((await build(member.api, project.id)).status).toBe('succeeded');
    for (const service of ['code-sandbox', 'portal-api']) {
      const found = standCompose('exec', '-T', service, 'sh', '-c', `find / -xdev -name '${mark}' 2>/dev/null || true`).toString('utf8');
      expect(found.trim(), `файл кода проекта в ${service}`).toBe('');
    }
  });
});

test.describe('Критерий 35: правила GPTool', () => {
  test('фабрика без одного из инструментов — находка tool_types с файлом и строкой; архива нет', async ({ newMember }) => {
    const member = await newMember();
    const { project } = await createWizardProject(member.api, 'MissingTool', [wizardTool({ title_en: 'First Step' }), wizardTool({ title_en: 'Second Step' })]);
    const factory = fileOf(project, 'GpToolFactory.cs');
    const content = factory.content.replace(', typeof(SecondStepTool)', '');
    expect(content).not.toBe(factory.content);
    await saveFile(member.api, project.id, factory, content);

    const result = await build(member.api, project.id);
    expect(result.status).toBe('succeeded');
    const finding = must(result.findings?.find((item) => item.rule === 'tool_types'), 'находка tool_types');
    expect(finding).toMatchObject({ file: 'GpToolFactory.cs', line: lineOf(content, 'GetToolTypes') });
    expect(finding.message).toMatch(CYRILLIC);
    expect(finding.message).toContain('SecondStepTool');
    expect(result.archive).toBeNull();
    const archive = await member.api.get(`/api/code/projects/${project.id}/build/archive`);
    expect(archive.status()).toBe(409);
    expect((await archive.json()).error.details.reason).toBe('findings');
  });

  test('инструмент без описания — находка tool_attributes у объявления класса', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'NoDescription');
    const tool = fileOf(project, 'AddressCheckTool.cs');
    const content = tool.content.replace(/^\s*\[GpDescription\("Проверка адреса", "Address Check"\)\]\n/m, '');
    expect(content).not.toBe(tool.content);
    await saveFile(member.api, project.id, tool, content);

    const result = await build(member.api, project.id);
    expect(result.status).toBe('succeeded');
    const finding = must(result.findings?.find((item) => item.rule === 'tool_attributes'), 'находка tool_attributes');
    expect(finding).toMatchObject({ file: 'AddressCheckTool.cs', line: lineOf(content, 'class AddressCheckTool') });
    expect(finding.message).toMatch(CYRILLIC);
  });

  test('выходной параметр с открытой записью — находка output_parameter у свойства', async ({ newMember }) => {
    const member = await newMember();
    const { project } = await createWizardProject(member.api, 'OpenOutput', [wizardTool({ outputs: [stringParameter('Result')] })]);
    const tool = fileOf(project, 'AddressCheckTool.cs');
    const content = tool.content
      .replace('public string? Result { get; private set; }', 'public string? Result { get; set; }')
      .replace('// Логика инструмента: прочитайте входные параметры и заполните выходные.', 'Result = "Готово";');
    expect(content).toContain('Result { get; set; }');
    expect(content).toContain('Result = "Готово";');
    await saveFile(member.api, project.id, tool, content);

    const result = await build(member.api, project.id);
    expect(result.status).toBe('succeeded');
    const finding = must(result.findings?.find((item) => item.rule === 'output_parameter'), 'находка output_parameter');
    expect(finding).toMatchObject({ file: 'AddressCheckTool.cs', line: lineOf(content, 'Result { get; set; }') });
    expect(finding.message).toMatch(CYRILLIC);
  });
});

test.describe('Критерий 36: архив для CoGIS и выгрузка для Visual Studio', () => {
  test('архив: DLL инструмента, недостающие на сервере библиотеки и памятка с путём и классом фабрики', async ({ newMember }) => {
    const member = await newMember();
    const { project } = await createWizardProject(member.api, 'ArchiveTool', [wizardTool({ uses_database: true })]);
    const result = await build(member.api, project.id);
    const archive = must(result.archive, 'архив');
    const sdk = await (await member.api.get('/api/code/sdk')).json();
    const onServer = (sdk.packages as { id: string; on_server: boolean }[]).filter((item) => item.on_server).map((item) => `${item.id}.dll`);
    expect(archive.on_server.sort()).toEqual(onServer.sort());

    const { response, files } = await downloadZip(member.api, `/api/code/projects/${project.id}/build/archive`);
    expect(response.headers()['content-disposition']).toContain("filename*=UTF-8''ArchiveTool-cogis.zip");
    expect([...files.keys()]).toEqual(archive.files.map((file) => file.file_name));
    for (const file of archive.files.filter((item) => item.kind !== 'memo')) {
      expect(must(files.get(file.file_name), file.file_name).subarray(0, 2).toString('latin1'), `${file.file_name} — сборка .NET`).toBe('MZ');
    }
    expect(archive.files.filter((file) => file.kind === 'package').length).toBeGreaterThan(0);
    for (const name of archive.on_server) {
      expect(files.has(name), `${name} есть на сервере и в архив не кладётся`).toBe(false);
    }
    const memo = must(files.get('Памятка.txt'), 'памятка');
    expect([...memo.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    const memoText = memo.toString('utf8');
    expect(memoText).toContain('/usr/elitegis/plugins/ArchiveTool.dll');
    expect(memoText).toContain('ArchiveTool.GpToolFactory');
    expect(memoText).toContain('Проверка адреса');
  });

  test('после изменения файла архив не отдаётся: сборка устарела', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'OutdatedTool');
    await build(member.api, project.id);
    const tool = fileOf(project, 'AddressCheckTool.cs');
    await saveFile(member.api, project.id, tool, `${tool.content}\n// изменено`);
    expect(must((await getProject(member.api, project.id)).build, 'сборка').outdated).toBe(true);
    const archive = await member.api.get(`/api/code/projects/${project.id}/build/archive`);
    expect(archive.status()).toBe(409);
    expect((await archive.json()).error.details.reason).toBe('outdated');
  });

  test('выгрузка: папка проекта, файлы, .csproj с настройками песочницы и ссылками на DLL CoGIS, памятка', async ({ newMember }) => {
    const member = await newMember();
    const { project } = await createWizardProject(member.api, 'ExportTool', [wizardTool({ uses_database: true })]);
    const { response, files } = await downloadZip(member.api, `/api/code/projects/${project.id}/export`);
    expect(response.headers()['content-disposition']).toContain("filename*=UTF-8''ExportTool.zip");
    for (const file of project.files) {
      expect(files.get(`ExportTool/${file.path}`)?.toString('utf8')).toBe(file.content);
    }
    const csproj = must(files.get('ExportTool/ExportTool.csproj'), '.csproj').toString('utf8');
    expect(csproj).toContain('<TargetFramework>net8.0</TargetFramework>');
    expect(csproj).toContain('<Nullable>enable</Nullable>');
    expect(csproj).toContain('<AssemblyName>ExportTool</AssemblyName>');
    expect(csproj).toMatch(/<Reference Include="eLiteGIS\.Server">\s*<HintPath>cogis\\eLiteGIS\.Server\.dll<\/HintPath>\s*<Private>false<\/Private>/);
    expect(csproj).toContain('<HintPath>cogis\\Atemiko.eLiteMap.dll</HintPath>');
    const memo = must(files.get('ExportTool/Прочтите.txt'), 'памятка выгрузки');
    expect([...memo.subarray(0, 3)]).toEqual([0xef, 0xbb, 0xbf]);
    expect(memo.toString('utf8')).toContain('eLiteGIS.Server.dll');
    expect([...files.keys()].some((name) => name.endsWith('.dll'))).toBe(false);
  });
});

test.describe('Критерий 37: помощник и правки', () => {
  test('помощник видит текущие файлы и выбранную схему; «только открытые файлы» ограничивает его', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'Sees');
    const mark = uniqueSuffix();
    const tool = fileOf(project, 'AddressCheckTool.cs');
    await saveFile(member.api, project.id, tool, `${tool.content}\n// метка ${mark}`);

    // Правило 2а: правка первого файла блока «проект» — с его текущим содержимым.
    const edit = editOf(await askCode(member.api, project.dialog_id, 'Добавь комментарий'));
    expect(edit.files).toEqual([{ path: 'AddressCheckTool.cs', change: 'modified', decision: null }]);
    const full = await (await member.api.get(`/api/code/projects/${project.id}/edits/${await lastAnswerId(member.api, project.dialog_id)}`)).json();
    expect(full.files[0].after).toContain(`// метка ${mark}`);
    expect(full.files[0].after).toContain('// Правка заглушки.');

    // Только открытый файл: первым и единственным в блоке «проект» идёт фабрика.
    const factory = fileOf(project, 'GpToolFactory.cs');
    const onlyOpen = editOf(await askCode(member.api, project.dialog_id, 'Поправь фабрику', 'write', { fileIds: [factory.id] }));
    expect(onlyOpen.files.map((file) => file.path)).toEqual(['GpToolFactory.cs']);

    // Схема проекта уходит модели: пометка ответа в тексте схемы срабатывает.
    const schemaId = await saveSqlSchema(member.api, `Схема ${mark}`, `CREATE TABLE contacts (id int); -- ${stubReply(`Схема видна ${mark}`)}`);
    const patched = await member.api.patch(`/api/code/projects/${project.id}`, { data: { schema_id: schemaId } });
    expect(patched.status(), await patched.text()).toBe(200);
    const events = await askCode(member.api, project.dialog_id, 'Что в схеме?', 'explain');
    expect(answerText(events)).toBe(`Схема видна ${mark}`);
    expect(eventsOf(events, 'code_edit')).toEqual([]);
  });

  test('правка меняет файлы только после «Принять»; принимается по файлам; решение одно', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'AcceptPart');
    const tool = fileOf(project, 'AddressCheckTool.cs');
    const changed = `${tool.content}\n// принятое изменение`;
    const added = `namespace ${project.name};\ninternal static class Helper { }`;
    const reply = stubReply(`Две правки.\n\n${editBlock('AddressCheckTool.cs', changed)}\n\n${editBlock('Helpers/Helper.cs', added)}`);

    const edit = editOf(await askCode(member.api, project.dialog_id, reply));
    expect(edit.status).toBe('proposed');
    expect(edit.files).toEqual([
      { path: 'AddressCheckTool.cs', change: 'modified', decision: null },
      { path: 'Helpers/Helper.cs', change: 'added', decision: null },
    ]);
    const untouched = await getProject(member.api, project.id);
    expect(untouched.files.map((file) => [file.path, file.content])).toEqual(project.files.map((file) => [file.path, file.content]));

    const messageId = await lastAnswerId(member.api, project.dialog_id);
    const full = await (await member.api.get(`/api/code/projects/${project.id}/edits/${messageId}`)).json();
    expect(full.files[0]).toMatchObject({ after: `${changed}\n`, current: { revision: tool.revision, content: tool.content }, outdated: false });
    expect(full.files[1]).toMatchObject({ after: `${added}\n`, current: null });

    const decision = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
      data: { accept: [{ path: 'AddressCheckTool.cs', revision: tool.revision }] },
    });
    expect(decision.status(), await decision.text()).toBe(200);
    const decided = await decision.json();
    expect(decided.edit.status).toBe('accepted');
    expect(decided.edit.files.map((file: { decision: string }) => file.decision)).toEqual(['accepted', 'rejected']);
    expect(fileOf(decided.project, 'AddressCheckTool.cs').content).toBe(`${changed}\n`);
    expect(decided.project.files.some((file: { path: string }) => file.path === 'Helpers/Helper.cs')).toBe(false);

    const snapshots = (await (await member.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items;
    expect(snapshots[0]).toMatchObject({ reason: 'edit_accepted', accepted_files: 1 });

    await expectError(
      await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, { data: { accept: [] } }),
      409,
      'edit_decided',
    );
  });

  test('правка принимается целиком; файл, изменённый после правки, не затирается молча', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'AcceptAll');
    const tool = fileOf(project, 'AddressCheckTool.cs');
    const reply = stubReply(`${editBlock('AddressCheckTool.cs', `${tool.content}\n// всё`)}\n${editBlock('Extra.cs', `namespace ${project.name};\ninternal static class Extra { }`)}`);
    await askCode(member.api, project.dialog_id, reply);
    const messageId = await lastAnswerId(member.api, project.dialog_id);

    // Файл изменён в редакторе после правки: решение со старой ревизией отклоняется.
    const saved = await saveFile(member.api, project.id, tool, `${tool.content}\n// вручную`);
    const stale = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
      data: { accept: [{ path: 'AddressCheckTool.cs', revision: tool.revision }, { path: 'Extra.cs', revision: null }] },
    });
    expect(stale.status()).toBe(409);
    expect((await stale.json()).error).toMatchObject({ code: 'edit_outdated', details: { paths: ['AddressCheckTool.cs'] } });

    const decision = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
      data: { accept: [{ path: 'AddressCheckTool.cs', revision: saved.revision }, { path: 'Extra.cs', revision: null }] },
    });
    expect(decision.status(), await decision.text()).toBe(200);
    const { project: after } = await decision.json();
    expect(fileOf(after, 'AddressCheckTool.cs').content).toBe(`${tool.content}\n// всё\n`);
    expect(fileOf(after, 'Extra.cs').revision).toBe(1);
  });

  test('путь вне проекта и недопустимое расширение в ответе модели отклоняются', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'RejectPaths');
    const body = 'namespace X;\ninternal static class Y { }';
    const reply = stubReply(
      [
        editBlock('../Secret.cs', body),
        editBlock('/etc/Passwd.cs', body),
        editBlock('C:/Windows/Evil.cs', body),
        editBlock('..\\Up.cs', body),
        editBlock('notes.txt', body),
        editBlock('Good.cs', `namespace ${project.name};\ninternal static class Good { }`),
      ].join('\n\n'),
    );
    const edit = editOf(await askCode(member.api, project.dialog_id, reply));
    expect(edit.files.map((file) => file.path)).toEqual(['Good.cs']);
    expect(edit.rejected_files).toEqual([
      { path: '../Secret.cs', reason: 'outside_project' },
      { path: '/etc/Passwd.cs', reason: 'outside_project' },
      { path: 'C:/Windows/Evil.cs', reason: 'outside_project' },
      { path: '..\\Up.cs', reason: 'outside_project' },
      { path: 'notes.txt', reason: 'extension_not_allowed' },
    ]);
    // Принять можно только принятое сервером.
    const messageId = await lastAnswerId(member.api, project.dialog_id);
    const forged = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
      data: { accept: [{ path: '../Secret.cs', revision: null }] },
    });
    expect(forged.status()).toBe(422);
    const accepted = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
      data: { accept: [{ path: 'Good.cs', revision: null }] },
    });
    expect(accepted.status(), await accepted.text()).toBe(200);
    expect((await accepted.json()).project.files.map((file: { path: string }) => file.path).sort()).toEqual(['AddressCheckTool.cs', 'Good.cs', 'GpToolFactory.cs']);
  });

  test('«Исправить ошибки сборки»: исправленная правка проверена сборкой, файлы не меняются до «Принять»', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'FixOnce');
    const fixed = `namespace ${project.name};\n\ninternal static class Broken\n{\n    internal static string Value() => "ok";\n}`;
    await addFile(member.api, project.id, 'A1Broken.cs', brokenFile(project.name));
    await addFile(member.api, project.id, 'Z9Fix.cs', `namespace ${project.name};\n/* [[stub:code-fix]]${editBlock('A1Broken.cs', fixed)}[[/stub:code-fix]] */`);
    expect((await build(member.api, project.id)).status).toBe('failed');

    const events = await askCode(member.api, project.dialog_id, 'Исправь ошибки сборки', 'fix', { timeout: 120_000 });
    const edit = editOf(events);
    expect(edit.verification).toMatchObject({ status: 'succeeded', builds: 2, diagnostics: [], findings: [] });
    expect(edit.files.map((file) => file.path)).toEqual(['A1Broken.cs']);
    const full = await (await member.api.get(`/api/code/projects/${project.id}/edits/${await lastAnswerId(member.api, project.dialog_id)}`)).json();
    expect(full.files[0].after).toBe(`${fixed}\n`);
    expect(fileOf(await getProject(member.api, project.id), 'A1Broken.cs').content).toBe(brokenFile(project.name));
  });

  test('«Исправить ошибки сборки» делает не больше двух дополнительных попыток', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'FixLimit');
    // Попытка 1 берёт пометку из A1Broken.cs, попытка 2 — из Z9Fix.cs (в A1Broken.cs её уже нет);
    // обе дают новый, но снова не собирающийся файл — без предела цикл шёл бы дальше.
    const first = brokenFile(project.name, 'missingA');
    const second = brokenFile(project.name, 'missingB');
    await addFile(
      member.api,
      project.id,
      'A1Broken.cs',
      `${brokenFile(project.name, 'missing0')}\n/* [[stub:code-fix]]${editBlock('A1Broken.cs', first)}[[/stub:code-fix]] */`,
    );
    await addFile(member.api, project.id, 'Z9Fix.cs', `namespace ${project.name};\n/* [[stub:code-fix]]${editBlock('A1Broken.cs', second)}[[/stub:code-fix]] */`);
    expect((await build(member.api, project.id)).status).toBe('failed');

    const events = await askCode(member.api, project.dialog_id, 'Исправь ошибки сборки', 'fix', { timeout: 180_000 });
    const stages = eventsOf(events, 'code_fix').map((event) => `${event.data.stage}:${event.data.attempt}/${event.data.max_attempts}`);
    expect(stages).toEqual(['building:0/2', 'fixing:1/2', 'building:1/2', 'fixing:2/2', 'building:2/2']);
    const edit = editOf(events);
    expect(edit.verification?.status).toBe('failed');
    expect(edit.verification?.builds).toBe(3);
    expect(edit.verification?.diagnostics.map((item) => item.message).join(' ')).toContain('missingB');
  });

  test('без ошибок в последней сборке исправлять нечего', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'NothingToFix');
    await build(member.api, project.id);
    const response = await member.api.post(`/api/dialogs/${project.dialog_id}/messages`, {
      data: { content: 'Исправь ошибки сборки', action: 'fix', file_ids: null, target: null },
    });
    await expectError(response, 409, 'nothing_to_fix');
  });

  test('успешная сборка и принятая правка создают версии; возврат версии работает и сохраняет текущее', async ({ newMember }) => {
    const member = await newMember();
    const project = await simpleProject(member.api, 'Versions');
    const tool = fileOf(project, 'AddressCheckTool.cs');
    expect((await (await member.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items).toEqual([]);

    await build(member.api, project.id);
    const manual = `${tool.content}\n// ручная правка`;
    await saveFile(member.api, project.id, tool, manual);
    let snapshots = (await (await member.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items;
    expect(snapshots.map((item: { reason: string }) => item.reason)).toEqual(['build_succeeded']);

    const restore = await member.api.post(`/api/code/projects/${project.id}/snapshots/${snapshots[0].id}/restore`);
    expect(restore.status(), await restore.text()).toBe(200);
    expect(fileOf(await restore.json(), 'AddressCheckTool.cs').content).toBe(tool.content);
    snapshots = (await (await member.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items;
    expect(snapshots.map((item: { reason: string }) => item.reason)).toEqual(['restored', 'before_restore', 'build_succeeded']);
    expect(snapshots[0].restored_from).not.toBeNull();

    // Ручная правка не потеряна: к ней можно вернуться.
    const back = await member.api.post(`/api/code/projects/${project.id}/snapshots/${snapshots[1].id}/restore`);
    expect(fileOf(await back.json(), 'AddressCheckTool.cs').content).toBe(manual);
    expect(must((await getProject(member.api, project.id)).build, 'сборка').outdated).toBe(true);

    // Принятая правка — тоже версия.
    await askCode(member.api, project.dialog_id, 'Добавь комментарий');
    const messageId = await lastAnswerId(member.api, project.dialog_id);
    const current = fileOf(await getProject(member.api, project.id), 'AddressCheckTool.cs');
    const decision = await member.api.post(`/api/code/projects/${project.id}/edits/${messageId}/decision`, {
      data: { accept: [{ path: 'AddressCheckTool.cs', revision: current.revision }] },
    });
    expect(decision.status(), await decision.text()).toBe(200);
    snapshots = (await (await member.api.get(`/api/code/projects/${project.id}/snapshots`)).json()).items;
    expect(snapshots[0]).toMatchObject({ reason: 'edit_accepted', accepted_files: 1 });
  });

  test('импорт ZIP из Visual Studio: берёт файлы .cs, снимает общую папку, сопоставляет пакеты', async ({ newMember }) => {
    const member = await newMember();
    const template = await simpleProject(member.api, 'Template');
    const text = (value: string) => Buffer.from(value, 'utf8');
    const csproj =
      '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup>' +
      '<ItemGroup><PackageReference Include="npgsql" Version="8.0.3" /><PackageReference Include="Dapper"><Version>2.1.35</Version></PackageReference></ItemGroup></Project>';
    const archive = zip([
      { path: 'Solution/Template/GpToolFactory.cs', data: text(fileOf(template, 'GpToolFactory.cs').content), deflate: true },
      { path: 'Solution/Template/Tools/AddressCheckTool.cs', data: text(fileOf(template, 'AddressCheckTool.cs').content) },
      { path: 'Solution/Template/Template.csproj', data: text(csproj) },
      { path: 'Solution/Template/bin/Debug/Generated.cs', data: text('garbage') },
      { path: 'Solution/Template/obj/AssemblyInfo.cs', data: text('garbage') },
      { path: 'Solution/.vs/Hidden.cs', data: text('garbage') },
      { path: 'Solution/Template/README.md', data: text('# readme') },
    ]);
    const response = await member.api.post('/api/code/projects/import', {
      multipart: { name: 'Imported', file: { name: 'Template.zip', mimeType: 'application/zip', buffer: archive } },
    });
    expect(response.status(), await response.text()).toBe(201);
    const { project, import: report } = await response.json();
    expect(project.files.map((file: { path: string }) => file.path).sort()).toEqual(['GpToolFactory.cs', 'Tools/AddressCheckTool.cs']);
    expect(project.packages).toEqual(['Npgsql']);
    expect(report.skipped_files).toEqual([]);
    expect(report.ignored_files).toBeGreaterThanOrEqual(1);
    expect(report.unmatched_packages).toEqual(
      expect.arrayContaining([
        { id: 'Dapper', version: '2.1.35', offline_version: null },
        { id: 'npgsql', version: '8.0.3', offline_version: '9.0.3' },
      ]),
    );
    const result = await build(member.api, project.id);
    expect(result.status).toBe('succeeded');
    expect(result.factory).toBe('Template.GpToolFactory');
  });
});

test.describe('Исправления приёмки 0.9 (ПК 0.20)', () => {
  test('имя проекта, совпадающее с библиотекой .NET или пакетом набора, без учёта регистра — invalid_format', async ({ newMember }) => {
    const member = await newMember();
    const template = await simpleProject(member.api, 'Template');
    const archive = zip([{ path: 'GpToolFactory.cs', data: Buffer.from(fileOf(template, 'GpToolFactory.cs').content, 'utf8') }]);
    const attempts = [
      () => member.api.post('/api/code/projects', { data: { source: 'wizard', wizard: { name: 'Npgsql', tools: [wizardTool()] } } }),
      () => member.api.post('/api/code/projects', { data: { source: 'example', name: 'system' } }),
      () =>
        member.api.post('/api/code/projects/import', {
          multipart: { name: 'MSCORLIB', file: { name: 'a.zip', mimeType: 'application/zip', buffer: archive } },
        }),
    ];
    for (const attempt of attempts) {
      const response = await attempt();
      await expectError(response, 422, 'validation_error');
      const fields = (await response.json()).error.fields as { field: string; code: string }[];
      expect(fields.map((item) => item.code)).toContain('invalid_format');
    }
    const names = (await (await member.api.get('/api/code/projects')).json()).items.map((item: { name: string }) => item.name);
    expect(names).toEqual(['Template']);
  });

  test('импорт ZIP: больше 100 пропущенных — список до 100, всего в skipped_total', async ({ newMember }) => {
    const member = await newMember();
    const template = await simpleProject(member.api, 'Template');
    const parts = [{ path: 'GpToolFactory.cs', data: Buffer.from(fileOf(template, 'GpToolFactory.cs').content, 'utf8') }];
    for (let index = 0; index < 150; index += 1) parts.push({ path: `Bad Name ${index}.cs`, data: Buffer.from('// x', 'utf8') });
    const response = await member.api.post('/api/code/projects/import', {
      multipart: { name: 'ManySkipped', file: { name: 'many.zip', mimeType: 'application/zip', buffer: zip(parts) } },
    });
    expect(response.status(), await response.text()).toBe(201);
    const { project, import: report } = await response.json();
    expect(project.files.map((file: { path: string }) => file.path)).toEqual(['GpToolFactory.cs']);
    expect(report.skipped_total).toBe(150);
    expect(report.skipped_files).toHaveLength(100);
    expect(new Set(report.skipped_files.map((item: { reason: string }) => item.reason))).toEqual(new Set(['invalid_path']));
  });
});

test('вторая сборка кнопкой во время идущей — 429, первая доводится до конца', async ({ newMember }) => {
  const member = await newMember();
  const project = await simpleProject(member.api, 'TwoClicks');
  const first = buildRaw(member.api, project.id);
  await expect.poll(async () => (await getProject(member.api, project.id)).build_running, { intervals: [50] }).toBe(true);
  await expectError(await buildRaw(member.api, project.id), 429, 'build_in_progress');
  expect((await first).status()).toBe(200);
});
