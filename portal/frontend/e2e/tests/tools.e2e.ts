/**
 * Критерии 7, 9 (на заглушке) и 10 (docs/portal-design.md §10): SQL-помощник со схемой,
 * объяснением чужого запроса и предупреждением об опасной операции; разбор документа с
 * таблицей реквизитов; экспорт ответа и диалога в DOCX.
 */
import type { Page } from '@playwright/test';

import { uniqueSuffix } from '../support/accounts';
import { docxText, pngImage, textPdf } from '../support/files';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { askChat, createDialog, eventsOf, expectError, parseStream, stubReply } from '../support/portal';
import { chooseDocparseFile, expectAnswerReady, send, toast } from '../support/ui';

const SQL_WRITE = /Опишите словами, что нужно получить/;
const SQL_EXPLAIN = /Вставьте запрос — объясню/;

function waitMessageRequest(page: Page) {
  return page.waitForRequest((request) => /\/api\/dialogs\/[^/]+\/messages$/.test(request.url()));
}

test.describe('SQL-помощник', () => {
  test('пишет запрос по выбранной схеме; синтаксис ответа проверен', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, '/sql');
    const schemaName = `Кадастр ${uniqueSuffix()}`;

    // Сотрудник сохраняет схему своей базы.
    await page.getByRole('button', { name: 'Мои схемы' }).click();
    const panel = page.getByRole('dialog');
    await expect(panel).toContainText('Схемы видите только вы');
    await expect(panel).toContainText('Сохранённых схем пока нет');
    await panel.getByRole('button', { name: 'Добавить схему' }).click();
    await panel.getByRole('textbox', { name: 'Название' }).fill(schemaName);
    await panel
      .getByRole('textbox', { name: 'Описание таблиц' })
      .fill('CREATE TABLE parcels (id bigint PRIMARY KEY, cadastral_number text, area_ha numeric, geom geometry);');
    await panel.getByRole('button', { name: 'Сохранить' }).click();
    await expect(toast(page, 'Схема сохранена')).toBeVisible();
    await expect(panel.getByText(schemaName)).toBeVisible();
    await panel.getByRole('button', { name: 'Закрыть модальное окно' }).click();

    await page.getByRole('button', { name: 'Схема базы' }).click();
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: schemaName }).click();
    await expect(page.getByRole('button', { name: 'Схема базы' })).toContainText(schemaName);

    const schemas = await (await member.api.get('/api/sql/schemas')).json();
    const schemaId: string = schemas.items[0].id;

    const request = waitMessageRequest(page);
    const reply = 'Запрос:\n```sql\nSELECT cadastral_number FROM parcels WHERE area_ha > 1;\n```';
    await send(page, `Участки площадью больше гектара ${stubReply(reply)}`, SQL_WRITE);
    expect((await request).postDataJSON()).toMatchObject({ action: 'write', dialect: 'postgres', schema_id: schemaId });
    await expectAnswerReady(page);

    await expect(page.locator('pre').filter({ hasText: 'SELECT cadastral_number FROM parcels' })).toBeVisible();
    await expect(page.getByText('Синтаксис проверен')).toBeVisible();
    await expect(page.getByText(/^Осторожно:/)).toHaveCount(0);
    await expect(page.getByText('Портал не выполняет запросы и не подключается к вашим базам', { exact: true })).toBeVisible();
  });

  test('объясняет чужой запрос и предупреждает об опасной операции в нём', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, '/sql');

    await page.getByRole('switch', { name: 'Объяснить запрос' }).click();
    const request = waitMessageRequest(page);
    await send(page, 'DELETE FROM parcels;', SQL_EXPLAIN);
    expect((await request).postDataJSON()).toMatchObject({ action: 'explain', schema_id: null });
    await expectAnswerReady(page);

    // Запрос дошёл до модели, а под вопросом — предупреждение о вставленном запросе.
    await expect(page.locator('main')).toContainText('Заглушка. Вопрос: «DELETE FROM parcels;»');
    await expect(
      page.getByRole('status').filter({
        hasText: 'Осторожно: в запросе нет условия WHERE — будут удалены все строки таблицы',
      }),
    ).toBeVisible();

    // Запрос сохранён в истории SQL-помощника и не попал в историю чата.
    await expect(page.getByRole('complementary', { name: 'История' }).getByRole('link')).toHaveCount(1);
    const chats = await (await member.api.get('/api/dialogs?kind=chat')).json();
    expect(chats.items).toEqual([]);
  });

  test('опасные операции и ошибка синтаксиса в ответе модели подсвечены', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'sql');
    const reply = [
      'Первый вариант:',
      '```sql',
      'UPDATE parcels SET area_ha = 0;',
      '```',
      'Второй вариант:',
      '```sql',
      'DROP TABLE parcels; TRUNCATE TABLE owners;',
      '```',
      'Третий вариант:',
      '```sql',
      'SELECT id FORM parcels WHERE;',
      '```',
    ].join('\n');
    const response = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: `Очисти площадь ${stubReply(reply)}`, action: 'write', dialect: 'postgres', schema_id: null },
    });
    const check = must(eventsOf(parseStream(await response.text()), 'sql_check')[0], 'событие sql_check').data;
    expect(check.blocks.map((block: { dangers: string[] }) => block.dangers)).toEqual([
      ['update_without_where'],
      ['drop', 'truncate'],
      [],
    ]);
    expect(check.blocks.map((block: { valid: boolean }) => block.valid)).toEqual([true, true, false]);

    const page = await pageAs(member, `/sql/${dialogId}`);
    await expectAnswerReady(page);
    const main = page.locator('main');
    await expect(main).toContainText('Осторожно: в запросе нет условия WHERE — будут изменены все строки таблицы');
    await expect(main).toContainText('Осторожно: запрос удаляет таблицу или другой объект базы целиком');
    await expect(main).toContainText('Осторожно: запрос удаляет все строки таблицы');
    await expect(main).toContainText(/Проверка нашла возможную ошибку: строка 1, позиция \d+/);
  });

  test('панель «Мои схемы» блокирующая: нажатие мимо не закрывает и не стирает набранное, фокус возвращается', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const page = await pageAs(member, '/sql');
    const opener = page.getByRole('button', { name: 'Мои схемы' });
    await opener.focus();
    await page.keyboard.press('Enter');
    const panel = page.getByRole('dialog');
    await panel.getByRole('button', { name: 'Добавить схему' }).click();
    const name = panel.getByRole('textbox', { name: 'Название' });
    await name.fill('Незаконченная схема');

    // Нажатие по затемнённому фону слева от панели.
    await page.mouse.click(200, 450);
    await expect(panel).toBeVisible();
    await expect(name).toHaveValue('Незаконченная схема');

    // Фокус с клавиатуры из панели не уходит.
    for (let step = 0; step < 12; step += 1) {
      await page.keyboard.press('Tab');
      expect(await page.evaluate(() => document.activeElement?.closest('[role="dialog"]') !== null)).toBe(true);
    }

    await page.keyboard.press('Escape');
    await expect(panel).toHaveCount(0);
    await expect(opener).toBeFocused();
  });

  test('чужая или удалённая схема не принимается', async ({ newMember }) => {
    const owner = await newMember();
    const other = await newMember();
    const schema = await (
      await owner.api.post('/api/sql/schemas', { data: { name: 'Моя схема', content: 'CREATE TABLE t (id int);' } })
    ).json();
    const dialogId = await createDialog(other.api, 'sql');
    const rejected = await other.api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: 'Напиши запрос', action: 'write', dialect: 'postgres', schema_id: schema.id },
    });
    await expectError(rejected, 422, 'schema_not_found');
  });
});

test.describe('Разбор документов', () => {
  test('PDF разбирается по шаблону: таблица реквизитов, краткое содержание, вопрос по документу', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const mark = uniqueSuffix();
    const pdf = textPdf(`lease-${mark}.pdf`, [`Lease agreement ${mark} between the parties for a land parcel.`]);
    const page = await pageAs(member, '/documents');

    // Без файла и шаблона разбор не начинается.
    await page.getByRole('button', { name: 'Разобрать' }).click();
    await expect(page.getByRole('alert').filter({ hasText: 'Выберите файл' })).toBeVisible();
    await expect(page.getByRole('alert').filter({ hasText: 'Выберите, что это за документ' })).toBeVisible();

    await chooseDocparseFile(page, pdf);
    await page.getByText('Договор аренды — Стороны, предмет, срок и плата по договору аренды').click();
    await page.getByRole('button', { name: 'Разобрать' }).click();

    await expect(page).toHaveURL(/\/documents\/[0-9a-f-]+$/);
    await expect(page.getByRole('heading', { name: 'Реквизиты' })).toBeVisible();
    const table = page.getByRole('table');
    for (const [field, value] of [
      ['Номер договора', 'заглушка: contract_number'],
      ['Арендодатель', 'заглушка: lessor'],
      ['Кадастровый номер', 'заглушка: cadastral_number'],
      ['Арендная плата', 'заглушка: rent'],
    ] as const) {
      await expect(table.getByRole('row').filter({ hasText: field })).toContainText(value);
    }
    await expect(
      page.getByText('Сверьте значения с оригиналом: модель может ошибиться в цифрах, датах и фамилиях.'),
    ).toBeVisible();
    await expect(page.getByRole('heading', { name: 'Краткое содержание' })).toBeVisible();
    await expect(page.locator('main')).toContainText(`Lease agreement ${mark}`);

    // Вопрос по документу — в том же разборе.
    await send(page, 'Кто арендатор?', 'Спросите о документе…');
    await expectAnswerReady(page);
    await expect(page.locator('main')).toContainText('Кто арендатор?');

    // Разбор сохранён в истории и открывается после перезагрузки.
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Реквизиты' })).toBeVisible();
    await expect(page.getByRole('complementary', { name: 'История' }).getByRole('link', { name: pdf.name })).toBeVisible();
    await expect(page.locator('main')).toContainText('Кто арендатор?');
  });

  test('реквизит, которого нет в документе, остаётся пустым, а не придумывается', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const mark = uniqueSuffix();
    // Ответ «модели» лежит в самом документе: арендатор и арендная плата в нём не названы.
    const extracted = {
      contract_number: `N-${mark}`,
      contract_date: '',
      lessor: 'Administration',
      lessee: '',
      subject: 'Land parcel',
      cadastral_number: '47:14:1203001:814',
      term: '49 years',
      rent: '',
    };
    const pdf = textPdf(`lease-${mark}.pdf`, [`Lease ${mark}. ${stubReply(JSON.stringify(extracted))}`]);
    const page = await pageAs(member, '/documents');
    await chooseDocparseFile(page, pdf);
    await page.getByText('Договор аренды — Стороны, предмет, срок и плата по договору аренды').click();
    await page.getByRole('button', { name: 'Разобрать' }).click();

    const table = page.getByRole('table');
    await expect(table.getByRole('row').filter({ hasText: 'Номер договора' })).toContainText(`N-${mark}`);
    await expect(table.getByRole('row').filter({ hasText: 'Кадастровый номер' })).toContainText('47:14:1203001:814');
    await expect(table.getByRole('row').filter({ hasText: 'Арендатор' })).toContainText('нет в документе');
    await expect(table.getByRole('row').filter({ hasText: 'Арендная плата' })).toContainText('нет в документе');
    await expect(table.getByRole('row').filter({ hasText: 'Арендодатель' })).not.toContainText('нет в документе');
  });

  test('скан-изображение разбирается через распознавание (заглушка)', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const mark = uniqueSuffix();
    const scan = pngImage(`выписка-${mark}.png`, mark);

    const response = await member.api.post('/api/docparse', { multipart: { file: scan, template_id: 'egrn' } });
    expect(response.status(), await response.text()).toBe(200);
    const events = parseStream(await response.text());
    expect(events.map((event) => event.event)).toEqual(expect.arrayContaining(['progress', 'extraction', 'done']));
    const extraction = must(eventsOf(events, 'extraction')[0], 'событие extraction').data;
    expect(extraction.fields.length).toBeGreaterThan(0);

    const page = await pageAs(member, `/documents/${extraction.dialog_id}`);
    await expect(page.getByRole('heading', { name: 'Реквизиты' })).toBeVisible();
    await expect(page.getByRole('table').getByRole('row').nth(1)).toContainText('заглушка:');
  });

  test('файл неподдерживаемого типа разбор не принимает', async ({ newMember }) => {
    const member = await newMember();
    const rejected = await member.api.post('/api/docparse', {
      multipart: {
        file: { name: 'заметка.txt', mimeType: 'text/plain', buffer: Buffer.from('Текстовый файл разбору не подходит') },
        template_id: 'free',
      },
    });
    await expectError(rejected, 415, 'unsupported_file_type');
  });
});

test.describe('Экспорт в DOCX', () => {
  const reply = [
    '## Итог проверки',
    '',
    'Срок аренды — 49 лет.',
    '',
    '- первый пункт',
    '- второй пункт',
    '',
    '| Участок | Площадь |',
    '|---|---|',
    '| 47:14:1203001:814 | 1,5 га |',
    '',
    '```sql',
    'SELECT 1;',
    '```',
  ].join('\n');

  test('ответ скачивается файлом DOCX с заголовком, списком, таблицей и кодом @браузер', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    await askChat(member.api, dialogId, `Вопрос об аренде ${stubReply(reply)}`);
    const title = `Договор аренды ${uniqueSuffix()}`;
    await member.api.patch(`/api/dialogs/${dialogId}`, { data: { title } });

    const page = await pageAs(member, `/chat/${dialogId}`);
    await expectAnswerReady(page);
    const download = page.waitForEvent('download');
    await page.getByRole('button', { name: 'Скачать в DOCX' }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe(`${title}.docx`);

    const chunks: Buffer[] = [];
    for await (const chunk of await file.createReadStream()) {
      chunks.push(chunk as Buffer);
    }
    const text = docxText(Buffer.concat(chunks));
    for (const expected of ['Итог проверки', 'Срок аренды — 49 лет.', 'второй пункт', '47:14:1203001:814', 'SELECT 1;']) {
      expect(text).toContain(expected);
    }
    // Один ответ — без вопроса; разметка Markdown в файл не просочилась.
    expect(text).not.toContain('Вопрос об аренде');
    expect(text).not.toContain('|---|');
    expect(text).not.toContain('```');
  });

  test('диалог целиком: вопросы и ответы с пометками, имя файла — название диалога @браузер', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    await askChat(member.api, dialogId, `Первый вопрос ${stubReply('Первый ответ.')}`);
    await askChat(member.api, dialogId, `Второй вопрос ${stubReply('Второй ответ.')}`);
    const title = `Переписка об аренде ${uniqueSuffix()}`;
    await member.api.patch(`/api/dialogs/${dialogId}`, { data: { title } });

    const page = await pageAs(member, `/chat/${dialogId}`);
    await expectAnswerReady(page);
    await page.locator('header').getByRole('button', { name: `Действия: ${title}` }).click();
    const download = page.waitForEvent('download');
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Скачать в DOCX' }).click();
    const file = await download;
    expect(file.suggestedFilename()).toBe(`${title}.docx`);

    const chunks: Buffer[] = [];
    for await (const chunk of await file.createReadStream()) {
      chunks.push(chunk as Buffer);
    }
    const text = docxText(Buffer.concat(chunks));
    for (const expected of ['Вы', 'Ответ', 'Первый вопрос', 'Первый ответ.', 'Второй вопрос', 'Второй ответ.']) {
      expect(text).toContain(expected);
    }
  });

  test('заголовки ответа сервера: тип DOCX, скачивание, русское имя файла', async ({ newMember }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    await askChat(member.api, dialogId, `Вопрос ${stubReply('Ответ для файла.')}`);
    await member.api.patch(`/api/dialogs/${dialogId}`, { data: { title: 'Выписка: участок 47/14?' } });

    const response = await member.api.get(`/api/dialogs/${dialogId}/export`);
    expect(response.status()).toBe(200);
    expect(response.headers()['content-type']).toBe(
      'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    );
    const disposition = must(response.headers()['content-disposition'], 'заголовок Content-Disposition');
    expect(disposition).toMatch(/^attachment; filename="dialog\.docx"; filename\*=UTF-8''/);
    const name = decodeURIComponent(disposition.split("filename*=UTF-8''")[1] ?? '');
    expect(name).toMatch(/^Выписка.*участок.*\.docx$/);
    // Символы, недопустимые в имени файла, заменены.
    expect(name).not.toMatch(/[:/?]/);
    expect(docxText(await response.body())).toContain('Ответ для файла.');
  });

  test('пустой диалог экспортировать нечем', async ({ newMember }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    await expectError(await member.api.get(`/api/dialogs/${dialogId}/export`), 409, 'nothing_to_export');
  });
});
