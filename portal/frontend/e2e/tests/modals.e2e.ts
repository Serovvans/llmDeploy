/**
 * Окна-формы не закрываются нажатием на фон (docs/portal-ui.md §4.5): случайное нажатие мимо
 * окна не должно стирать введённое и выбранные файлы. Закрывают их «Отмена», крестик и Esc.
 */
import type { Locator, Page } from '@playwright/test';

import { uniqueSuffix } from '../support/accounts';
import { textFile } from '../support/files';
import { expect, test } from '../support/fixtures';
import { searchUsers } from '../support/ui';

/** Нажатие по фону в углу экрана, вне окна. */
async function clickBackground(page: Page, modal: Locator): Promise<void> {
  const box = await modal.locator('[data-tid~="modal-content"]').or(modal).first().boundingBox();
  expect(box === null || box.x > 30 || box.y > 30, 'окно не занимает угол экрана').toBe(true);
  await page.mouse.click(8, 8);
}

test('«Новый пользователь» и «Изменить учётную запись»: фон не закрывает, Esc и «Отмена» закрывают', async ({
  newMember,
  pageAs,
}) => {
  const admin = await newMember('admin');
  const employee = await newMember();
  const page = await pageAs(admin, '/admin/users');
  const modal = page.getByRole('dialog');

  await page.getByRole('button', { name: 'Добавить пользователя' }).click();
  const fullName = modal.getByRole('textbox', { name: 'Фамилия, имя, отчество' });
  await fullName.fill('Набранное Не Пропадает');
  await clickBackground(page, modal);
  await expect(modal).toContainText('Новый пользователь');
  await expect(fullName).toHaveValue('Набранное Не Пропадает');
  await page.keyboard.press('Escape');
  await expect(modal).toHaveCount(0);

  await searchUsers(page, employee.account.login);
  await page.getByRole('button', { name: `Действия: ${employee.account.fullName}` }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: /^Изменить$/ }).click();
  await modal.getByRole('textbox', { name: 'Фамилия, имя, отчество' }).fill('Правка Не Пропадает');
  await clickBackground(page, modal);
  await expect(modal).toContainText('Изменить учётную запись');
  await expect(modal.getByRole('textbox', { name: 'Фамилия, имя, отчество' })).toHaveValue('Правка Не Пропадает');
  await modal.getByRole('button', { name: 'Отмена' }).click();
  await expect(modal).toHaveCount(0);
  // Отмена ничего не сохранила.
  await expect(page.getByRole('row').filter({ hasText: employee.account.login })).toContainText(
    employee.account.fullName,
  );
});

test('«Сменить пароль»: фон не закрывает окно и не стирает набранное', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member, '/profile');
  const modal = page.getByRole('dialog');

  await page.getByRole('button', { name: 'Сменить пароль' }).click();
  const current = modal.getByRole('textbox', { name: /^Текущий пароль/ });
  await current.fill('набранный-пароль');
  await clickBackground(page, modal);
  await expect(current).toHaveValue('набранный-пароль');
  await page.keyboard.press('Escape');
  await expect(modal).toHaveCount(0);
});

test('«Добавить документы»: фон не закрывает окно и не сбрасывает выбранные файлы', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const file = textFile(`выбранный-${uniqueSuffix()}.txt`, 'Файл выбран, но ещё не добавлен.');
  const page = await pageAs(member, '/knowledge');
  const modal = page.getByRole('dialog');

  await page.getByRole('button', { name: 'Добавить документы' }).first().click();
  await modal.locator('input[type=file]').setInputFiles(file);
  await expect(modal.getByText(file.name)).toBeVisible();
  await clickBackground(page, modal);
  await expect(modal.getByText(file.name)).toBeVisible();
  await modal.getByRole('button', { name: 'Закрыть', exact: true }).or(modal.getByRole('button', { name: 'Отмена' })).click();
  await expect(modal).toHaveCount(0);
  // Закрытие без «Добавить» ничего не загрузило.
  expect((await (await member.api.get('/api/kb/documents?scope=shared&q=выбранный')).json()).items).toEqual([]);
});
