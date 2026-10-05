/** Действия сотрудника в интерфейсе, общие для нескольких тестов. */
import { expect, type Locator, type Page } from '@playwright/test';

import { nextCode, type Account } from './accounts';
import type { TestFile } from './files';

export function composer(page: Page, placeholder: string | RegExp = 'Спросите что-нибудь…'): Locator {
  return page.getByRole('textbox', { name: placeholder });
}

/**
 * Отправляет вопрос из поля ввода текущего раздела. В SQL-помощнике кнопка получает имя
 * «Отправить», только когда загрузились настройки, — нажатие само дожидается этого.
 */
export async function send(page: Page, text: string, placeholder?: string | RegExp): Promise<void> {
  await composer(page, placeholder).fill(text);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
}

/**
 * Чат получил настройки портала (`GET /api/config`): у поля выбора файлов появился список
 * расширений. От настроек зависят проверки до отправки — длина сообщения, тип и размер файла.
 */
export async function expectChatConfigLoaded(page: Page): Promise<void> {
  await expect(page.locator('input[type=file]')).toHaveAttribute('accept', /\.pdf/);
}

/** Ждёт, пока ответ сформирован до конца. */
export async function expectAnswerReady(page: Page): Promise<void> {
  await expect(page.getByRole('status').filter({ hasText: 'Ответ готов' })).toBeAttached();
  await expect(page.getByRole('button', { name: 'Ответить заново' })).toBeVisible();
}

/** Прикрепляет файлы к сообщению чата, когда интерфейс знает пределы вложений. */
export async function attachFiles(page: Page, files: TestFile | TestFile[]): Promise<void> {
  await expectChatConfigLoaded(page);
  await page.locator('input[type=file]').setInputFiles(files);
}

/** Выбирает файл для разбора; шаблоны на экране значат, что интерфейс получил конфигурацию. */
export async function chooseDocparseFile(page: Page, file: TestFile): Promise<void> {
  await expect(page.getByRole('radio')).not.toHaveCount(0);
  await page.locator('input[type=file]').setInputFiles(file);
}

export async function fillLogin(page: Page, login: string, password: string): Promise<void> {
  await page.getByRole('textbox', { name: 'Логин' }).fill(login);
  await page.getByRole('textbox', { name: /^Пароль/ }).fill(password);
  await page.getByRole('button', { name: 'Войти' }).click();
}

/** Обычный вход в интерфейсе: логин, пароль и код из «приложения». */
export async function signInThroughUi(page: Page, account: Account): Promise<void> {
  await page.goto('/login');
  await fillLogin(page, account.login, account.password);
  await expect(page.getByRole('heading', { name: 'Код из приложения' })).toBeVisible();
  await page.getByRole('textbox', { name: 'Код из приложения' }).fill(await nextCode(account));
  await expect(page.getByRole('navigation', { name: 'Разделы портала' })).toBeVisible();
}

export async function signOutThroughUi(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Профиль' }).click();
  await page.getByRole('menuitem', { name: 'Выйти' }).or(page.getByText('Выйти', { exact: true })).first().click();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
}

/** Всплывающее сообщение библиотеки компонентов. */
export function toast(page: Page, text: string | RegExp): Locator {
  return page.locator('[data-tid~="ToastView__root"]').filter({ hasText: text });
}
