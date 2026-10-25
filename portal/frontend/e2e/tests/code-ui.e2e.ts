/**
 * Экран «Код» в браузере (docs/portal-design.md §10, критерии 33, 34, 36, 37; docs/portal-ui.md §5.13):
 * мастер, панель «Сборка», подчёркивание ошибки в редакторе, окно архива, разница и «Принять».
 * Данные, которые экран только показывает, готовятся через API.
 */
import { addFile, askCode, brokenFile, fileOf, getProject, simpleProject } from '../support/code';
import { zip } from '../support/files';
import { expect, test } from '../support/fixtures';

for (const colorScheme of ['light', 'dark'] as const) {
  test(`мастер «Новый GPTool» создаёт проект, он собирается, окно архива показывает путь и класс фабрики (${colorScheme})`, async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const page = await pageAs(member, '/code');
    await page.emulateMedia({ colorScheme });
    await page.goto('/code/new');

    await page.getByLabel('Имя проекта').fill('UiTool');
    await page.getByLabel('Название по-русски').fill('Проверка адреса');
    await page.getByLabel('Название по-английски').fill('Address Check');
    await page.getByLabel('Категория').fill('Утилиты');
    await page.getByRole('button', { name: 'Создать проект' }).click();
    await expect(page).toHaveURL(/\/code\/[0-9a-f-]{36}$/);

    const panel = page.getByRole('region', { name: 'Сборка' });
    await panel.getByRole('button', { name: 'Собрать', exact: true }).click();
    await expect(panel.getByText('Собрано', { exact: true })).toBeVisible({ timeout: 30_000 });

    await panel.getByRole('button', { name: 'Скачать для CoGIS' }).click();
    const dialog = page.getByRole('dialog');
    await expect(dialog.getByText('/usr/elitegis/plugins/UiTool.dll')).toBeVisible();
    await expect(dialog.getByText('UiTool.GpToolFactory')).toBeVisible();
    await expect(dialog.getByText('UiTool.dll — инструмент')).toBeVisible();
  });
}

test('ошибка компиляции — в панели «Сборка» с файлом и строкой на русском и подчёркнута в редакторе', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await simpleProject(member.api, 'UiBroken');
  await addFile(member.api, project.id, 'Broken.cs', brokenFile(project.name));
  const page = await pageAs(member, `/code/${project.id}`);

  const panel = page.getByRole('region', { name: 'Сборка' });
  await panel.getByRole('button', { name: 'Собрать', exact: true }).click();
  await expect(panel.getByText('Не собрано · 1')).toBeVisible({ timeout: 30_000 });
  if (!(await panel.getByRole('button', { name: /^Ошибка: Broken\.cs, строка 5: / }).isVisible())) {
    await panel.getByRole('button', { name: 'Показать сообщения' }).click();
  }
  const message = panel.getByRole('button', { name: /^Ошибка: Broken\.cs, строка 5: / });
  await expect(message).toBeVisible();
  await expect(message).toContainText(/[а-яё]/i);
  await expect(message).toContainText('connStr');

  // Переход к месту ошибки открывает файл, и Monaco подчёркивает ошибку.
  await message.click();
  await expect(page.locator('.monaco-editor .squiggly-error').first()).toBeAttached({ timeout: 15_000 });
});

test('правка помощника показывается разницей и меняет файл только после «Принять всё»', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const project = await simpleProject(member.api, 'UiAccept');
  await askCode(member.api, project.dialog_id, 'Добавь комментарий');
  const page = await pageAs(member, `/code/${project.id}`);

  await page.getByRole('button', { name: 'Посмотреть и принять' }).click();
  await expect(page.getByRole('heading', { name: 'Правка помощника · 1 файл' })).toBeVisible();
  await expect(page.locator('.monaco-diff-editor').first()).toBeVisible({ timeout: 15_000 });
  expect(fileOf(await getProject(member.api, project.id), 'AddressCheckTool.cs').content).not.toContain('// Правка заглушки.');

  await page.getByRole('button', { name: 'Принять всё' }).click();
  await expect(page.getByText('Правка принята: 1 файл')).toBeVisible();
  expect(fileOf(await getProject(member.api, project.id), 'AddressCheckTool.cs').content).toContain('// Правка заглушки.');
});

test('мастер: имя проекта, занятое пакетом набора, — отказ с текстом про библиотеки', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member, '/code/new');
  await page.getByLabel('Имя проекта').fill('npgsql');
  await page.getByLabel('Название по-русски').fill('Проверка адреса');
  await page.getByLabel('Название по-английски').fill('Address Check');
  await page.getByLabel('Категория').fill('Утилиты');
  await page.getByRole('button', { name: 'Создать проект' }).click();
  await expect(
    page.getByText('Так назвать проект нельзя: „npgsql“ — служебное слово C#, имя, занятое в коде каркаса или библиотекой (.NET или пакетом)', {
      exact: false,
    }),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/code\/new$/);
});

test('загрузка ZIP: «Не взяты файлы» считается по skipped_total, а не по списку из 100', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const template = await simpleProject(member.api, 'Template');
  const parts = [{ path: 'GpToolFactory.cs', data: Buffer.from(fileOf(template, 'GpToolFactory.cs').content, 'utf8') }];
  for (let index = 0; index < 150; index += 1) parts.push({ path: `Bad Name ${index}.cs`, data: Buffer.from('// x', 'utf8') });
  const page = await pageAs(member, '/code');

  await page.getByRole('button', { name: 'Загрузить ZIP' }).first().click();
  const dialog = page.getByRole('dialog');
  await dialog.locator('input[type=file]').setInputFiles({ name: 'many.zip', mimeType: 'application/zip', buffer: zip(parts) });
  await dialog.getByLabel('Имя проекта').fill('ManySkipped');
  await dialog.getByRole('button', { name: 'Загрузить', exact: true }).click();

  await expect(page).toHaveURL(/\/code\/[0-9a-f-]{36}$/);
  await expect(page.getByText('Не взяты файлы: 150')).toBeVisible();
  await expect(page.getByText('и ещё 145', { exact: true })).toBeVisible();
});
