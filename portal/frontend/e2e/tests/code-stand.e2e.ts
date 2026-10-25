/**
 * Раздел «Код»: проверки, меняющие состояние всего стенда (docs/portal-design.md §10, критерии 34 и 37;
 * docs/portal-api.md §7.5.7, §7.5.15): флаги раздела и сборки, остановленная песочница, предел
 * одновременных сборок и снятие зависшей сборки по времени. `portal-api` перезапускается с
 * накладкой, в которой изменён только раздел `code:`, и после теста возвращается к накладке стенда.
 */
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  addFile,
  askCode,
  brokenFile,
  build,
  buildRaw,
  downloadZip,
  editOf,
  fileOf,
  getProject,
  saveFile,
  simpleProject,
  wizardTool,
} from '../support/code';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { createDialog, expectError } from '../support/portal';
import { restartApiWith, standCompose, STATE_DIR } from '../support/stand';

const SANDBOX_DOCKERFILE = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../code-sandbox/Dockerfile');

/**
 * Файл, который Roslyn связывает экспоненциально долго: вложенные лямбды с перегрузками. На стенде
 * глубина 8 собирается за ~3 с, каждый уровень — примерно в 1,7 раза дольше; глубина 13 — минуты.
 */
function slowFile(namespace: string, depth = 13): string {
  let expression = '0';
  for (let level = depth - 1; level >= 0; level -= 1) {
    expression = `F(p${level} => ${expression})`;
  }
  return [
    'using System;',
    `namespace ${namespace};`,
    'internal static class Slow',
    '{',
    '    static int F(Func<int, int> f) => 0;',
    '    static int F(Func<long, int> f) => 0;',
    '    static int F(Func<short, int> f) => 0;',
    '    static int F(Func<byte, int> f) => 0;',
    `    internal static int G() => ${expression};`,
    '}',
  ].join('\n');
}

test('раздел «Код» выключен: /api/code — 403, проекты остаются, помощник CoGIS работает как в 0.8; после включения прежний диалог только для чтения @стенд', async ({
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const project = await simpleProject(member.api, 'KeptProject');
  // Пока раздел включён, прежние диалоги CoGIS только для чтения: новый не создаётся.
  const refused = await member.api.post('/api/dialogs', { data: { kind: 'cogis' } });
  expect(refused.status()).toBe(422);

  let created: string | undefined;
  const restore = restartApiWith('code:\n  enabled: false\n  build:\n    enabled: true\n');
  try {
    expect((await (await member.api.get('/api/config')).json()).code.enabled).toBe(false);
    await expectError(await member.api.get('/api/code/projects'), 403, 'feature_disabled');
    await expectError(await member.api.get(`/api/code/projects/${project.id}`), 403, 'feature_disabled');
    await expectError(await member.api.post(`/api/code/projects/${project.id}/build`), 403, 'feature_disabled');
    await expectError(await member.api.get(`/api/dialogs/${project.dialog_id}`), 403, 'feature_disabled');

    created = await createDialog(member.api, 'cogis');
    const answer = await member.api.post(`/api/dialogs/${created}/messages`, { data: { content: 'Как написать плагин?', action: 'write' } });
    expect(answer.status(), await answer.text()).toBe(200);
    expect(await answer.text()).toContain('event: done');

    const page = await pageAs(member, '/code');
    await expect(page).toHaveURL(/\/cogis$/);
    await expect(page.getByRole('heading', { name: 'Помощник CoGIS' })).toBeVisible();
  } finally {
    restore();
  }
  // После включения проект на месте.
  expect((await getProject(member.api, project.id)).name).toBe('KeptProject');

  // Прежний диалог CoGIS — только для чтения: читается, выгружается, переносится в «Код», удаляется.
  const dialogId = must(created, 'прежний диалог CoGIS');
  const listed = (await (await member.api.get('/api/dialogs?kind=cogis')).json()).items as { id: string }[];
  expect(listed.map((item) => item.id)).toEqual([dialogId]);
  expect((await member.api.get(`/api/dialogs/${dialogId}/messages`)).status()).toBe(200);
  expect((await member.api.get(`/api/dialogs/${dialogId}/export`)).status()).toBe(200);
  await expectError(await member.api.post(`/api/dialogs/${dialogId}/messages`, { data: { content: 'Ещё вопрос', action: 'write' } }), 409, 'dialog_read_only');
  await expectError(await member.api.post(`/api/dialogs/${dialogId}/regenerate`), 409, 'dialog_read_only');
  await expectError(await member.api.patch(`/api/dialogs/${dialogId}`, { data: { title: 'Новое' } }), 409, 'dialog_read_only');
  const carried = await member.api.post('/api/code/projects', {
    data: { source: 'wizard', wizard: { name: 'FromCogis', tools: [wizardTool()] }, carried: { text: 'Сведения из CoGIS', source_dialog_id: dialogId } },
  });
  expect(carried.status(), await carried.text()).toBe(201);

  const page = await pageAs(member, `/cogis/${dialogId}`);
  await expect(page).toHaveURL(new RegExp(`/code/dialogs/${dialogId}$`));
  await expect(page.getByText('Только для чтения').first()).toBeVisible();
  expect((await member.api.delete(`/api/dialogs/${dialogId}`)).status()).toBe(204);
});

test('сборка выключена: сборка и архив — 403, итог прежней сборки виден, редактор, помощник и выгрузка работают @стенд', async ({
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const project = await simpleProject(member.api, 'BuildOff');
  await addFile(member.api, project.id, 'Broken.cs', brokenFile(project.name));
  expect((await build(member.api, project.id)).status).toBe('failed');

  const restore = restartApiWith('code:\n  build:\n    enabled: false\n');
  try {
    expect((await (await member.api.get('/api/config')).json()).code.build_enabled).toBe(false);
    await expectError(await buildRaw(member.api, project.id), 403, 'feature_disabled');
    await expectError(await member.api.get(`/api/code/projects/${project.id}/build/archive`), 403, 'feature_disabled');
    const current = await getProject(member.api, project.id);
    expect(must(current.build, 'прежняя сборка').status).toBe('failed');

    // Цикл исправления не собирает: правка помечена «не проверена».
    const fix = editOf(await askCode(member.api, project.dialog_id, 'Исправь ошибки сборки', 'fix'));
    expect(fix.verification).toEqual({ status: 'unavailable', builds: 0, warnings: 0, diagnostics: [], findings: [] });
    const tool = fileOf(current, 'AddressCheckTool.cs');
    await saveFile(member.api, project.id, tool, `${tool.content}\n// без сборки`);
    expect(editOf(await askCode(member.api, project.dialog_id, 'Добавь комментарий')).status).toBe('proposed');
    expect((await member.api.get(`/api/code/projects/${project.id}/export`)).status()).toBe(200);

    const page = await pageAs(member, `/code/${project.id}`);
    await expect(page.getByText('Сборка на портале выключена. Редактор и помощник работают, собрать проект можно в Visual Studio')).toBeVisible();
  } finally {
    restore();
  }
});

test('песочница остановлена: «сборка недоступна», прежний итог и архив остаются, редактор и помощник работают; после запуска сборка идёт без перезапуска портала @стенд', async ({
  newMember,
  pageAs,
}) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const project = await simpleProject(member.api, 'SandboxDown');
  expect((await build(member.api, project.id)).status).toBe('succeeded');

  standCompose('stop', 'code-sandbox');
  try {
    await expectError(await buildRaw(member.api, project.id), 503, 'build_unavailable');
    const current = await getProject(member.api, project.id);
    expect(must(current.build, 'прежняя сборка').status).toBe('succeeded');
    expect((await member.api.get(`/api/code/projects/${project.id}/build/archive`)).status()).toBe(200);
    expect(editOf(await askCode(member.api, project.dialog_id, 'Добавь комментарий')).status).toBe('proposed');

    const page = await pageAs(member, `/code/${project.id}`);
    await page.getByRole('button', { name: 'Собрать', exact: true }).click();
    await expect(page.getByText('Сборка недоступна', { exact: true }).first()).toBeVisible();

    const tool = fileOf(current, 'AddressCheckTool.cs');
    await saveFile(member.api, project.id, tool, `${tool.content}\n// без песочницы`);
  } finally {
    standCompose('up', '-d', '--no-build', '--wait', 'code-sandbox');
  }
  expect((await build(member.api, project.id)).status).toBe('succeeded');
});

test('одновременных сборок не больше предела, зависшая снимается по времени и не держит песочницу @стенд', async ({ newMember }) => {
  test.setTimeout(240_000);
  const member = await newMember();
  const slow = await simpleProject(member.api, 'SlowBuild');
  await addFile(member.api, slow.id, 'Slow.cs', slowFile(slow.name));
  const waiting = await simpleProject(member.api, 'WaitingBuild');
  const after = await simpleProject(member.api, 'AfterBuild');

  const restore = restartApiWith('code:\n  build:\n    enabled: true\n    max_parallel: 1\n    wait_seconds: 2\n    timeout_seconds: 6\n');
  try {
    const started = Date.now();
    const hanging = buildRaw(member.api, slow.id);
    await expect.poll(async () => (await getProject(member.api, slow.id)).build_running, { intervals: [100] }).toBe(true);

    // Место одно и занято: вторая сборка ждёт не дольше wait_seconds и получает отказ.
    const queuedAt = Date.now();
    await expectError(await buildRaw(member.api, waiting.id), 503, 'build_unavailable');
    expect(Date.now() - queuedAt).toBeGreaterThanOrEqual(1_800);

    const hung = await hanging;
    expect(hung.status(), await hung.text()).toBe(200);
    expect((await hung.json()).status).toBe('timeout');
    const elapsed = Date.now() - started;
    expect(elapsed).toBeGreaterThanOrEqual(5_500);
    expect(elapsed).toBeLessThan(20_000);
    expect(must((await getProject(member.api, slow.id)).build, 'итог').status).toBe('timeout');

    // Песочница свободна сразу после снятия: следующая сборка не ждёт зависшую.
    expect((await build(member.api, after.id)).status).toBe('succeeded');
  } finally {
    restore();
  }
});

test('выгруженный проект собирается .NET SDK без сети — как в Visual Studio, с DLL CoGIS в папке cogis @стенд', async ({ newMember }) => {
  test.setTimeout(240_000);
  const member = await newMember();
  const project = await simpleProject(member.api, 'VsExport');
  const { files } = await downloadZip(member.api, `/api/code/projects/${project.id}/export`);
  const root = mkdtempSync(path.join(STATE_DIR, 'vs-export-'));
  try {
    for (const [name, data] of files) {
      mkdirSync(path.dirname(path.join(root, name)), { recursive: true });
      writeFileSync(path.join(root, name), data);
    }
    // Памятка выгрузки: DLL CoGIS копируются в папку cogis рядом с .csproj. На стенде это заглушка SDK.
    standCompose('cp', 'code-sandbox:/stub-sdk/.', path.join(root, 'VsExport', 'cogis'));
    const image = must(/^FROM (mcr\.microsoft\.com\/dotnet\/sdk:\S+)/m.exec(readFileSync(SANDBOX_DOCKERFILE, 'utf8'))?.[1], 'образ .NET SDK');
    const output = execFileSync(
      'docker',
      ['run', '--rm', '--network', 'none', '-v', `${root}:/src`, '-w', '/src/VsExport', image, 'dotnet', 'build', '-nologo', '-c', 'Release'],
      { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] },
    );
    expect(output).toMatch(/0 Error\(s\)|0 Ошибок/);
    expect(existsSync(path.join(root, 'VsExport', 'bin', 'Release', 'net8.0', 'VsExport.dll'))).toBe(true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});
