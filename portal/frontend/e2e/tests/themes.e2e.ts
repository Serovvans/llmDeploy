/**
 * Критерий 11 (docs/portal-design.md §10): светлая и тёмная темы на всех экранах, все тексты
 * на русском. Каждый экран открывается в обеих темах; проверяются цвета страницы и крупных
 * поверхностей, а текст экрана — на отсутствие нерусских слов. Снимки экранов сохраняются в
 * каталог результатов теста — для дизайн-ревью.
 */
import type { Page, TestInfo } from '@playwright/test';

import { adoptSession, createUser, nextCode, uniqueSuffix } from '../support/accounts';
import { pngImage, textFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { addKbDocument, askChat, createDialog, eventsOf, parseStream, stubReply } from '../support/portal';
import { expectAnswerReady, fillLogin, searchUsers } from '../support/ui';

type Theme = 'light' | 'dark';

/** Слова латиницей, которые в русском интерфейсе на своём месте: названия форматов, продуктов, клавиш. */
const ALLOWED_LATIN = [
  'SQL',
  'CoGIS',
  'DOCX',
  'PDF',
  'TXT',
  'MD',
  'JPG',
  'PNG',
  'C#',
  'PostgreSQL',
  'PostGIS',
  'Enter',
  'Shift',
  'QR',
  'Google Authenticator',
  // Ключевое слово в предупреждении SQL-помощника.
  'WHERE',
];

interface Surface {
  theme: string | undefined;
  colorScheme: string;
  background: number;
  contrast: number;
  /** Крупные поверхности, цвет которых принадлежит противоположной теме. */
  alien: string[];
}

/** Цвета экрана: яркость фона страницы, контраст текста и крупные поверхности «не той» темы. */
function readSurface(page: Page, theme: Theme): Promise<Surface> {
  return page.evaluate((expected) => {
    const probe = document.createElement('canvas').getContext('2d', { willReadFrequently: true });
    if (!probe) {
      throw new Error('Нет двумерного холста для чтения цветов');
    }
    const rgba = (color: string): [number, number, number, number] => {
      probe.clearRect(0, 0, 1, 1);
      probe.fillStyle = color;
      probe.fillRect(0, 0, 1, 1);
      const [r = 0, g = 0, b = 0, a = 0] = probe.getImageData(0, 0, 1, 1).data;
      return [r, g, b, a / 255];
    };
    const luminance = ([r, g, b]: [number, number, number, number]): number => {
      const channel = (value: number) => {
        const share = value / 255;
        return share <= 0.03928 ? share / 12.92 : ((share + 0.055) / 1.055) ** 2.4;
      };
      return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
    };
    const body = getComputedStyle(document.body);
    const background = luminance(rgba(body.backgroundColor));
    const text = luminance(rgba(body.color));
    const contrast = (Math.max(background, text) + 0.05) / (Math.min(background, text) + 0.05);

    const alien: string[] = [];
    for (const element of document.body.querySelectorAll<HTMLElement>('*')) {
      // QR-код по контракту рисуется на белой подложке в любой теме; изображения — содержимое, а не тема.
      if (element.closest('svg, img, [class*="qr" i]') || element.querySelector(':scope > svg[role="img"]')) {
        continue;
      }
      const box = element.getBoundingClientRect();
      if (box.width * box.height < 20_000 || !element.checkVisibility()) {
        continue;
      }
      const color = rgba(getComputedStyle(element).backgroundColor);
      if (color[3] < 0.9) {
        continue;
      }
      const value = luminance(color);
      if ((expected === 'dark' && value > 0.7) || (expected === 'light' && value < 0.15)) {
        alien.push(`${element.tagName.toLowerCase()}.${String(element.className).slice(0, 40)}`);
      }
    }
    return {
      theme: document.documentElement.dataset.theme,
      colorScheme: document.documentElement.style.colorScheme,
      background,
      contrast,
      alien,
    };
  }, theme);
}

/** Слова латиницей в тексте экрана, которых там быть не должно. */
async function foreignWords(page: Page, dataWords: string[]): Promise<string[]> {
  let text = await page.locator('body').innerText();
  // Длинные слова убираются первыми: иначе «SQL» оставил бы от «PostgreSQL» обрубок.
  for (const word of [...ALLOWED_LATIN, ...dataWords].sort((a, b) => b.length - a.length)) {
    text = text.split(word).join(' ');
  }
  // Логины и метки тестовых данных.
  text = text.replace(/e2e-[a-z0-9-]+/g, ' ').replace(/\b[0-9a-f]{8,10}\b/g, ' ');
  return [...new Set(text.match(/[A-Za-z][A-Za-z'-]+/g) ?? [])];
}

async function checkScreen(page: Page, theme: Theme, name: string, testInfo: TestInfo, dataWords: string[] = []) {
  await test.step(`${name} — ${theme === 'dark' ? 'тёмная' : 'светлая'} тема`, async () => {
    // Экран уже дождался своего содержимого; остаются шрифты, от которых зависит снимок.
    await page.evaluate(() => document.fonts.ready.then(() => undefined));
    const surface = await readSurface(page, theme);
    expect.soft(surface.theme, `${name}: тема страницы`).toBe(theme);
    expect.soft(surface.colorScheme, `${name}: color-scheme`).toBe(theme);
    if (theme === 'dark') {
      expect.soft(surface.background, `${name}: фон страницы тёмный`).toBeLessThan(0.1);
    } else {
      expect.soft(surface.background, `${name}: фон страницы светлый`).toBeGreaterThan(0.85);
    }
    expect.soft(surface.contrast, `${name}: контраст текста и фона`).toBeGreaterThan(7);
    expect.soft(surface.alien, `${name}: поверхности противоположной темы`).toEqual([]);
    expect.soft(await foreignWords(page, dataWords), `${name}: слова не на русском`).toEqual([]);
    expect.soft(await page.locator('html').getAttribute('lang'), `${name}: язык страницы`).toBe('ru');
    await page.screenshot({ path: testInfo.outputPath(`${name}.png`), fullPage: true });
  });
}

/** Данные для экранов: чат с ответом и источником, запрос SQL с предупреждением, разбор документа. */
async function prepareData(member: Member) {
  const mark = `zq${uniqueSuffix()}`;
  const document = textFile(`регламент-${mark}.txt`, `Регламент ${mark} о сроке аренды: сорок девять лет.`);
  await addKbDocument(member.api, document, { scope: 'personal' });

  const chatId = await createDialog(member.api, 'chat');
  await askChat(member.api, chatId, `${mark} ${stubReply('Срок аренды — сорок девять лет [1].')}`, {
    knowledge: 'shared_and_personal',
  });
  await member.api.patch(`/api/dialogs/${chatId}`, { data: { title: 'Срок аренды участка' } });

  const sqlId = await createDialog(member.api, 'sql');
  const sql = await member.api.post(`/api/dialogs/${sqlId}/messages`, {
    data: {
      content: `Удали участки ${stubReply('Запрос:\n```sql\nDELETE FROM parcels;\n```')}`,
      action: 'write',
      dialect: 'postgres',
      schema_id: null,
    },
  });
  await sql.text();
  await member.api.patch(`/api/dialogs/${sqlId}`, { data: { title: 'Удаление участков' } });

  const parse = await member.api.post('/api/docparse', {
    multipart: {
      file: pngImage(`договор-${mark}.png`, mark.slice(2)),
      template_id: 'lease',
    },
  });
  const docparseId: string = must(eventsOf(parseStream(await parse.text()), 'extraction')[0], 'событие extraction')
    .data.dialog_id;
  return { mark, document, chatId, sqlId, docparseId };
}

for (const theme of ['light', 'dark'] as const) {
  const themeName = theme === 'dark' ? 'тёмная' : 'светлая';

  test(`${themeName} тема: экраны входа, смены пароля и настройки второго фактора`, async ({
    adminApi,
    browser,
  }, testInfo) => {
    const context = await browser.newContext({ colorScheme: theme === 'dark' ? 'light' : 'dark' });
    // Выбор темы хранится в браузере и сильнее темы системы (она здесь противоположная).
    await context.addInitScript((value) => window.localStorage.setItem('portal.theme', value), theme);
    const page = await context.newPage();
    const user = await createUser(adminApi);
    const password = `новый-пароль-${user.login}`;

    await page.goto('/login');
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
    await checkScreen(page, theme, 'вход', testInfo);

    await fillLogin(page, user.login, 'неверный-пароль-000');
    await expect(page.getByRole('alert')).toHaveText('Неверный логин или пароль');
    await checkScreen(page, theme, 'вход-ошибка', testInfo);

    await fillLogin(page, user.login, user.temporaryPassword);
    await expect(page.getByRole('heading', { name: 'Придумайте новый пароль' })).toBeVisible();
    await checkScreen(page, theme, 'смена-пароля', testInfo);

    await page.getByRole('textbox', { name: /^Новый пароль/ }).fill(password);
    await page.getByRole('textbox', { name: /^Повторите пароль/ }).fill(password);
    await page.getByRole('button', { name: 'Сохранить пароль' }).click();
    await expect(page.getByRole('img', { name: 'QR-код для приложения-аутентификатора' })).toBeVisible();
    await page.getByRole('button', { name: 'Не получается отсканировать' }).click();
    const secretText = await page.locator('.p-mono').first().innerText();
    await checkScreen(page, theme, 'настройка-второго-фактора', testInfo, [secretText, ...secretText.split(/\s+/)]);

    // QR-код читается в любой теме: тёмные модули на белой подложке.
    const qr = page.getByRole('img', { name: 'QR-код для приложения-аутентификатора' });
    const qrColors = await qr.evaluate((svg) => {
      const path = svg.querySelector('path');
      let holder: Element | null = svg;
      while (holder && getComputedStyle(holder).backgroundColor === 'rgba(0, 0, 0, 0)') {
        holder = holder.parentElement;
      }
      return {
        modules: path ? getComputedStyle(path).fill : null,
        backing: holder ? getComputedStyle(holder).backgroundColor : null,
      };
    });
    expect.soft(qrColors, 'QR-код: тёмные модули на белой подложке').toEqual({
      modules: 'rgb(0, 0, 0)',
      backing: 'rgb(255, 255, 255)',
    });

    const account = { secret: secretText.replace(/\s/g, ''), lastStep: 0 };
    await page.getByRole('textbox', { name: 'Введите код из приложения' }).fill(await nextCode(account));
    await expect(page.getByRole('heading', { name: 'Сохраните резервные коды' })).toBeVisible();
    const codes = await page.getByText(/^[0-9A-Z]{4}-[0-9A-Z]{4}$/).allInnerTexts();
    await checkScreen(page, theme, 'резервные-коды', testInfo, codes.flatMap((code) => [code, ...code.split('-')]));

    await page.getByText('Я сохранил(а) коды').click();
    await page.getByRole('button', { name: 'Перейти к работе' }).click();
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
    await page.getByRole('button', { name: 'Профиль' }).click();
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Выйти' }).click();
    await fillLogin(page, user.login, password);
    await expect(page.getByRole('heading', { name: 'Код из приложения' })).toBeVisible();
    await checkScreen(page, theme, 'ввод-кода', testInfo);
    await page.getByRole('button', { name: 'Ввести резервный код' }).click();
    await expect(page.getByRole('heading', { name: 'Резервный код' })).toBeVisible();
    await checkScreen(page, theme, 'ввод-резервного-кода', testInfo);
    await context.close();
  });

  test(`${themeName} тема: рабочие экраны`, async ({ newMember, browser }, testInfo) => {
    test.setTimeout(180_000);
    const member = await newMember('admin');
    const data = await prepareData(member);
    const dataWords = [data.document.name, `договор-${data.mark}.png`, data.mark, 'DELETE FROM parcels;', 'sql'];

    const context = await browser.newContext({ colorScheme: theme === 'dark' ? 'light' : 'dark' });
    await context.addInitScript((value) => window.localStorage.setItem('portal.theme', value), theme);
    await adoptSession(context, member.api);
    const page = await context.newPage();

    await page.goto('/chat');
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
    await checkScreen(page, theme, 'чат-пустой', testInfo);

    await page.goto(`/chat/${data.chatId}`);
    await expectAnswerReady(page);
    // Пометка заглушки — часть текста вопроса.
    const stubEcho = ['stub', 'reply'];
    await checkScreen(page, theme, 'чат-ответ-с-источником', testInfo, [...dataWords, ...stubEcho]);

    await page.getByRole('button', { name: new RegExp(`^1\\s*${data.document.name}`) }).click();
    await expect(page.getByRole('dialog').getByRole('link', { name: 'Открыть оригинал' })).toBeVisible();
    await checkScreen(page, theme, 'просмотр-источника', testInfo, [...dataWords, ...stubEcho]);

    await page.goto('/knowledge');
    await page.getByText('Моя', { exact: true }).click();
    await expect(page.getByRole('row').filter({ hasText: data.document.name })).toBeVisible();
    await checkScreen(page, theme, 'база-знаний', testInfo, dataWords);
    await page.getByRole('button', { name: 'Добавить документы' }).first().click();
    await expect(page.getByRole('dialog')).toContainText('Куда добавить');
    await checkScreen(page, theme, 'база-знаний-добавление', testInfo, dataWords);

    await page.goto('/sql');
    await expect(page.getByRole('heading', { name: 'Опишите задачу — получите запрос' })).toBeVisible();
    await checkScreen(page, theme, 'sql-пустой', testInfo);
    await page.goto(`/sql/${data.sqlId}`);
    await expectAnswerReady(page);
    await expect(page.getByText(/^Осторожно:/).first()).toBeVisible();
    await checkScreen(page, theme, 'sql-ответ-с-предупреждением', testInfo, [...dataWords, 'stub', 'reply']);
    await page.getByRole('button', { name: 'Мои схемы' }).click();
    await expect(page.getByRole('dialog')).toContainText('Схемы видите только вы');
    await page.getByRole('dialog').getByRole('button', { name: 'Добавить схему' }).click();
    await checkScreen(page, theme, 'sql-схемы', testInfo, [...dataWords, 'stub', 'reply', 'CREATE TABLE']);

    await page.goto('/cogis');
    await expect(page.getByRole('heading', { name: 'Плагины CoGIS на C#' })).toBeVisible();
    await checkScreen(page, theme, 'cogis', testInfo);

    await page.goto('/documents');
    await expect(page.getByRole('button', { name: 'Разобрать' })).toBeVisible();
    await checkScreen(page, theme, 'разбор-форма', testInfo, dataWords);
    await page.goto(`/documents/${data.docparseId}`);
    await expect(page.getByRole('heading', { name: 'Реквизиты' })).toBeVisible();
    // Значения реквизитов и краткое содержание на стенде — ответ заглушки с именами полей шаблона.
    const stubFields = await page.getByRole('table').getByRole('cell').allInnerTexts();
    await checkScreen(page, theme, 'разбор-результат', testInfo, [...dataWords, ...stubFields]);

    await page.goto('/admin/users');
    await searchUsers(page, member.account.login);
    await expect(page.getByRole('row').filter({ hasText: member.account.login })).toHaveCount(1);
    await checkScreen(page, theme, 'пользователи', testInfo);
    await page.getByRole('button', { name: 'Добавить пользователя' }).click();
    await expect(page.getByRole('dialog')).toContainText('Новый пользователь');
    await checkScreen(page, theme, 'пользователи-создание', testInfo);

    await page.goto('/profile');
    await expect(page.getByRole('heading', { name: 'Профиль' })).toBeVisible();
    await checkScreen(page, theme, 'профиль', testInfo);

    await page.goto('/nosuch-page');
    await expect(page.getByRole('heading', { name: 'Страница не найдена' })).toBeVisible();
    await checkScreen(page, theme, 'страница-не-найдена', testInfo);
    await context.close();
  });
}

test('переключатель темы: выбор действует сразу, запоминается и переживает выход', async ({ newMember, browser }) => {
  const member = await newMember();
  const context = await browser.newContext({ colorScheme: 'light' });
  await adoptSession(context, member.api);
  const page = await context.newPage();
  await page.goto('/chat');
  const html = page.locator('html');
  await expect(html).toHaveAttribute('data-theme', 'light');

  await page.getByRole('button', { name: 'Тема' }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Тёмная' }).click();
  await expect(html).toHaveAttribute('data-theme', 'dark');
  expect((await readSurface(page, 'dark')).background).toBeLessThan(0.1);

  await page.reload();
  await expect(html).toHaveAttribute('data-theme', 'dark');
  await page.goto('/knowledge');
  await expect(html).toHaveAttribute('data-theme', 'dark');

  // Экран входа после выхода — в той же теме.
  await page.getByRole('button', { name: 'Профиль' }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Выйти' }).click();
  await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
  await expect(html).toHaveAttribute('data-theme', 'dark');
  expect((await readSurface(page, 'dark')).background).toBeLessThan(0.1);
  await context.close();
});

test('тема по умолчанию — как в системе, и следует за её сменой', async ({ newMember, browser }) => {
  const member = await newMember();
  const context = await browser.newContext({ colorScheme: 'dark' });
  await adoptSession(context, member.api);
  const page = await context.newPage();
  await page.goto('/chat');
  const html = page.locator('html');
  await expect(html).toHaveAttribute('data-theme', 'dark');

  await page.emulateMedia({ colorScheme: 'light' });
  await expect(html).toHaveAttribute('data-theme', 'light');
  expect((await readSurface(page, 'light')).background).toBeGreaterThan(0.85);

  // Явный выбор «Светлая» перестаёт следовать за системой; «Как в системе» возвращает.
  await page.getByRole('button', { name: 'Тема' }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Светлая' }).click();
  await page.emulateMedia({ colorScheme: 'dark' });
  await expect(html).toHaveAttribute('data-theme', 'light');
  await page.getByRole('button', { name: 'Тема' }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Как в системе' }).click();
  await expect(html).toHaveAttribute('data-theme', 'dark');
  await context.close();
});

test('ошибки API приходят с русским текстом', async ({ newMember }) => {
  const member = await newMember();
  const responses = [
    await member.api.get('/api/dialogs/00000000-0000-4000-8000-000000000000'),
    await member.api.get('/api/admin/users'),
    await member.api.post('/api/dialogs', { data: { kind: 'неизвестный' } }),
    await member.api.post('/api/auth/password', { data: { new_password: 'короткий', current_password: 'x' } }),
    await member.api.post('/api/kb/documents', { multipart: { scope: 'shared', is_cogis: 'false' } }),
  ];
  for (const response of responses) {
    const { error } = await response.json();
    expect(response.status(), error.code).toBeGreaterThanOrEqual(400);
    expect(error.message, error.code).toMatch(/[А-Яа-яЁё]/);
    expect(error.message, error.code).not.toMatch(/[A-Za-z]{2,}/);
    for (const field of error.fields ?? []) {
      expect(field.message, `${error.code}/${field.field}`).not.toMatch(/[A-Za-z]{2,}/);
    }
  }
});
