/**
 * Критерий 3 (docs/portal-design.md §10): чат отвечает потоково, принимает изображение и PDF,
 * хранит историю между входами. Плюс §5.1: остановка, повторная генерация, режимы ответа,
 * переименование и удаление, запоминание выбора базы знаний.
 */
import { uniqueSuffix } from '../support/accounts';
import { pngImage, textFile, textPdf } from '../support/files';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { answerText, askChat, chatWithAnswer, createDialog, eventsOf, expectError } from '../support/portal';
import {
  attachFiles,
  composer,
  expectAnswerReady,
  send,
  signInThroughUi,
  signOutThroughUi,
  toast,
} from '../support/ui';

test('ответ приходит потоком: текст появляется частями до завершения', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member);

  await send(page, 'Расскажи подробно [[stub:slow]]');
  // Ответ уже виден, а формирование ещё идёт: вместо «Отправить» — «Остановить».
  await expect(page.getByText('Заглушка. Вопрос: «Расскажи подробно').last()).toBeVisible();
  await expect(page.getByRole('button', { name: 'Остановить' })).toBeVisible();
  await expect(page.getByRole('status').filter({ hasText: 'Модель отвечает' })).toBeAttached();
  await expect(page.getByRole('button', { name: 'Ответить заново' })).toHaveCount(0);

  await page.getByRole('button', { name: 'Остановить' }).click();
  await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeVisible();
  await expect(page.getByRole('paragraph').filter({ hasText: /^Ответ остановлен$/ })).toBeVisible();

  // Остановленная часть ответа сохранена на сервере.
  const dialogId = must(page.url().split('/').pop(), 'идентификатор чата в адресе');
  await expect
    .poll(async () => {
      const messages = await (await member.api.get(`/api/dialogs/${dialogId}/messages`)).json();
      return messages.items[0].status;
    })
    .toBe('stopped');
});

test('поток событий: сначала размышления, затем текст несколькими частями, в конце — done', async ({ newMember }) => {
  const member = await newMember();
  const { events } = await chatWithAnswer(member.api, 'Сколько длится аренда участка под линию электропередачи?');
  expect(events[0]?.event).toBe('start');
  expect(eventsOf(events, 'reasoning_delta').length).toBeGreaterThan(0);
  expect(eventsOf(events, 'delta').length).toBeGreaterThan(1);
  expect(eventsOf(events, 'title')).toHaveLength(1);
  expect(events.at(-1)).toEqual({ event: 'done', data: { status: 'complete' } });
  expect(answerText(events)).toBe(
    'Заглушка. Вопрос: «Сколько длится аренда участка под линию электропередачи?». Изображений: 0.',
  );
});

test('чат принимает изображение и PDF; вложения видны в вопросе и доходят до модели', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const token = uniqueSuffix();
  const image = pngImage(`фото-${token}.png`, token);
  const pdf = textPdf(`договор-${token}.pdf`, [`Lease agreement ${token} between the parties for a land parcel.`]);
  const page = await pageAs(member);

  await attachFiles(page, [image, pdf]);
  await expect(page.getByRole('button', { name: `Убрать файл ${image.name}` })).toBeVisible();
  await expect(page.getByText(`${pdf.name} · 1 стр.`)).toBeVisible();
  await send(page, 'Что в этих файлах?');
  await expectAnswerReady(page);

  // Изображение ушло модели картинкой, PDF с текстовым слоем — текстом.
  const answer = page.locator('main');
  await expect(answer).toContainText('Изображений: 1.');
  await expect(answer).toContainText(`Lease agreement ${token}`);
  await expect(page.getByRole('button', { name: image.name })).toBeVisible();
  await expect(page.getByText(`${pdf.name} · 1 стр.`)).toBeVisible();

  // Изображение открывается в портале.
  await page.getByRole('button', { name: image.name }).click();
  const preview = page.getByRole('dialog').getByRole('img');
  await expect(preview).toBeVisible();
  await expect.poll(() => preview.evaluate((img: HTMLImageElement) => img.naturalWidth)).toBe(32);
});

test('неподходящий файл и скан длиннее восьми страниц отклоняются с объяснением', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member);
  await attachFiles(page, {
    name: 'таблица.xlsx',
    mimeType: 'application/octet-stream',
    buffer: Buffer.from('это не документ'),
  });
  await expect(page.getByRole('alert')).toContainText('Файл „таблица.xlsx“ не подходит');

  // Девять изображений в одном сообщении: модель видит не больше восьми, сервер не берёт «первые 8».
  const dialogId = await createDialog(member.api, 'chat');
  const attachmentIds: string[] = [];
  for (let index = 0; index < 9; index += 1) {
    const file = pngImage(`скан-${index}.png`, `${index}${uniqueSuffix()}`);
    const uploaded = await member.api.post(`/api/dialogs/${dialogId}/attachments`, { multipart: { file } });
    expect(uploaded.status()).toBe(201);
    attachmentIds.push((await uploaded.json()).id);
  }
  const rejected = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
    data: { content: 'Что на сканах?', mode: 'fast', knowledge: 'none', attachment_ids: attachmentIds },
  });
  await expectError(rejected, 422, 'too_many_images');
});

test('история хранится между входами', async ({ newMember, page }) => {
  const { account } = await newMember();
  const question = `Вопрос для истории ${uniqueSuffix()}`;
  await signInThroughUi(page, account);
  await send(page, question);
  await expectAnswerReady(page);
  const title = `Заглушка. Вопрос: «${question}». Изображений: 0.`;
  await expect(page.getByRole('heading', { name: title })).toBeVisible();

  await signOutThroughUi(page);
  await signInThroughUi(page, account);

  const history = page.getByRole('complementary', { name: 'История' });
  await expect(history.getByRole('heading', { name: 'Сегодня' })).toBeVisible();
  await history.getByRole('link', { name: title }).click();
  await expect(page.getByRole('paragraph').filter({ hasText: question }).first()).toBeVisible();
  await expect(page.getByText(title).last()).toBeVisible();
});

test('режим ответа: «Вдумчиво» уходит на сервер, размышления показаны свёрнутым блоком', async ({
  newMember,
  pageAs,
}) => {
  const member = await newMember();
  const page = await pageAs(member);
  await expect(page.getByRole('switch', { name: 'Быстро' })).toBeChecked();

  await page.getByRole('switch', { name: 'Вдумчиво' }).click();
  const request = page.waitForRequest((candidate) => /\/api\/dialogs\/[^/]+\/messages$/.test(candidate.url()));
  await send(page, 'Подумай как следует');
  expect((await request).postDataJSON()).toMatchObject({ mode: 'thorough', knowledge: 'none' });
  await expectAnswerReady(page);

  const thinking = page.getByRole('button', { name: /^Размышления/ });
  await expect(thinking).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByText('Размышление заглушки.')).toBeHidden();
  await thinking.click();
  await expect(page.getByText('Размышление заглушки.')).toBeVisible();

  await page.getByRole('switch', { name: 'Быстро' }).click();
  const second = page.waitForRequest((candidate) => /\/api\/dialogs\/[^/]+\/messages$/.test(candidate.url()));
  await send(page, 'А теперь быстро');
  expect((await second).postDataJSON()).toMatchObject({ mode: 'fast' });
});

test('«Ответить заново» заменяет последний ответ, а не добавляет второй', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const { dialogId } = await chatWithAnswer(member.api, 'Вопрос для повторного ответа');
  const page = await pageAs(member, `/chat/${dialogId}`);
  await expectAnswerReady(page);

  const regenerate = page.waitForResponse((response) => response.url().endsWith(`/api/dialogs/${dialogId}/regenerate`));
  await page.getByRole('button', { name: 'Ответить заново' }).click();
  expect((await regenerate).status()).toBe(200);
  await expectAnswerReady(page);

  const messages = await (await member.api.get(`/api/dialogs/${dialogId}/messages`)).json();
  expect(messages.items.map((message: { role: string }) => message.role)).toEqual(['assistant', 'user']);
});

test('чат переименовывается и удаляется из истории', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const { dialogId } = await chatWithAnswer(member.api, 'Вопрос для переименования');
  const page = await pageAs(member, `/chat/${dialogId}`);
  const title = 'Заглушка. Вопрос: «Вопрос для переименования». Изображений: 0.';
  const renamed = `Аренда участка ${uniqueSuffix()}`;
  const header = page.locator('header');

  await header.getByRole('button', { name: `Действия: ${title}` }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Переименовать' }).click();
  const titleField = page.getByRole('textbox', { name: 'Название чата' });
  await titleField.fill(renamed);
  await titleField.press('Enter');
  await expect(page.getByRole('heading', { name: renamed })).toBeVisible();
  await expect(page.getByRole('complementary', { name: 'История' }).getByRole('link', { name: renamed })).toBeVisible();

  await header.getByRole('button', { name: `Действия: ${renamed}` }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Удалить' }).click();
  await expect(page.getByText(`Удалить чат „${renamed}“?`)).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить' }).click();
  await expect(toast(page, 'Чат удалён')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
  await expectError(await member.api.get(`/api/dialogs/${dialogId}`), 404, 'not_found');
});

test('выбор базы знаний запоминается в браузере, по умолчанию — «Не использовать»', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member);
  await expect(page.getByRole('button', { name: 'База знаний: не использовать' })).toBeVisible();

  await page.getByRole('button', { name: 'База знаний: не использовать' }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: /^Общая и моя$/ }).click();
  await expect(page.getByRole('button', { name: 'База знаний: общая и моя' })).toBeVisible();

  await page.reload();
  await expect(page.getByRole('button', { name: 'База знаний: общая и моя' })).toBeVisible();
});

test('ответ с разметкой: таблица, список, блок кода с кнопкой «Скопировать»; HTML не исполняется', async ({
  newMember,
  pageAs,
}) => {
  const member = await newMember();
  const reply = [
    '## Итог',
    '',
    '| Участок | Площадь |',
    '|---|---|',
    '| 47:14:1203001:814 | 1,5 га |',
    '',
    '- первый пункт',
    '- второй пункт',
    '',
    '```sql',
    'SELECT 1;',
    '```',
    '',
    '<img src="x" onerror="window.__xss = 1"><script>window.__xss = 1</script>',
  ].join('\n');
  const dialogId = await createDialog(member.api, 'chat');
  await askChat(member.api, dialogId, `Покажи разметку [[stub:reply]]${reply}[[/stub:reply]]`);
  const page = await pageAs(member, `/chat/${dialogId}`);
  await expectAnswerReady(page);

  await expect(page.getByRole('heading', { name: 'Итог', exact: true })).toBeVisible();
  await expect(page.getByRole('cell', { name: '47:14:1203001:814' })).toBeVisible();
  await expect(page.getByRole('listitem').filter({ hasText: 'второй пункт' })).toBeVisible();
  await expect(page.locator('pre').filter({ hasText: 'SELECT 1;' })).toBeVisible();
  // «Скопировать» есть и у блока кода, и у ответа целиком.
  await expect(page.getByRole('button', { name: 'Скопировать' })).toHaveCount(2);
  await expect(page.locator('main img[src="x"]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { __xss?: number }).__xss)).toBeUndefined();
});

test('заголовки из ответа модели сдвинуты на два уровня: на экране один заголовок первого уровня', async ({
  newMember,
  pageAs,
}) => {
  const member = await newMember();
  const reply = ['# Первый', '## Второй', '### Третий', '#### Четвёртый', '###### Шестой', '', 'Текст.'].join('\n');
  const dialogId = await createDialog(member.api, 'chat');
  await askChat(member.api, dialogId, `Покажи заголовки [[stub:reply]]${reply}[[/stub:reply]]`);
  await member.api.patch(`/api/dialogs/${dialogId}`, { data: { title: 'Уровни заголовков' } });
  const page = await pageAs(member, `/chat/${dialogId}`);
  await expectAnswerReady(page);

  for (const [name, level] of [
    ['Первый', 3],
    ['Второй', 4],
    ['Третий', 5],
    ['Четвёртый', 6],
    ['Шестой', 6],
  ] as const) {
    await expect(page.getByRole('heading', { name, exact: true, level })).toBeVisible();
  }
  await expect(page.locator('h1')).toHaveCount(1);
  await expect(page.locator('h1')).toHaveText('Уровни заголовков');
  // Заголовок ответа не крупнее названия экрана.
  const sizes = await page.evaluate(() =>
    [...document.querySelectorAll('h1, h3')].map((heading) => parseFloat(getComputedStyle(heading).fontSize)),
  );
  expect(Math.max(...sizes.slice(1))).toBeLessThanOrEqual(sizes[0] ?? 0);
});

test('вложение TXT в кодировке Windows-1251 читается как русский текст', async ({ newMember }) => {
  const member = await newMember();
  const dialogId = await createDialog(member.api, 'chat');
  // «Срок аренды» в Windows-1251.
  const cp1251 = Buffer.from([0xd1, 0xf0, 0xee, 0xea, 0x20, 0xe0, 0xf0, 0xe5, 0xed, 0xe4, 0xfb]);
  const uploaded = await member.api.post(`/api/dialogs/${dialogId}/attachments`, {
    multipart: { file: { ...textFile('заметка.txt', ''), buffer: cp1251 } },
  });
  expect(uploaded.status(), await uploaded.text()).toBe(201);
  const events = await askChat(member.api, dialogId, 'Что в файле?', { attachmentIds: [(await uploaded.json()).id] });
  expect(answerText(events)).toContain('Срок аренды');
});

test('пустое сообщение не отправляется', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const page = await pageAs(member);
  await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  await expect(page.getByRole('alert')).toHaveText('Напишите вопрос');
  await expect(composer(page)).toBeFocused();
});
