/**
 * Критерии 4, 5 (на заглушке), 6 и 8 (docs/portal-design.md §10): общий документ находится
 * у другого сотрудника, ответ показывает источник с документом, страницей и цитатой;
 * личный документ другим не виден; помощник CoGIS ссылается на загруженную документацию.
 *
 * Общая база на стенде одна на все тесты, поэтому у каждого теста свои слова-метки:
 * по ним лексический поиск ставит документ теста первым источником.
 */
import type { Page } from '@playwright/test';

import { uniqueSuffix } from '../support/accounts';
import { pngImage, textFile, textPdf } from '../support/files';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { addKbDocument, chatWithAnswer, createDialog, eventsOf, expectError, parseStream } from '../support/portal';
import { expectAnswerReady, send, toast } from '../support/ui';

function token(): string {
  return `zq${uniqueSuffix()}`;
}

/** PDF из двух страниц; слово-метка второй страницы — `<метка>b`. */
function twoPagePdf(mark: string) {
  return textPdf(`reglament-${mark}.pdf`, [
    `First page of the regulation ${mark}a describes general provisions.`,
    `Second page says the lease term ${mark}b is forty nine years.`,
  ]);
}

async function chooseKnowledge(page: Page, option: 'Общая' | 'Общая и моя'): Promise<void> {
  await page.getByRole('button', { name: /^База знаний:/ }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: new RegExp(`^${option}$`) }).click();
}

test('документ, добавленный одним сотрудником в общую базу, находится в ответе другого; источник — документ, страница, цитата', async ({
  newMember,
  pageAs,
}) => {
  const author = await newMember();
  const reader = await newMember();
  const mark = token();
  const pdf = twoPagePdf(mark);

  // Автор добавляет документ в интерфейсе.
  const authorPage = await pageAs(author, '/knowledge');
  await authorPage.getByRole('button', { name: 'Добавить документы' }).first().click();
  const modal = authorPage.getByRole('dialog');
  await modal.locator('input[type=file]').setInputFiles(pdf);
  await expect(modal.getByText(pdf.name)).toBeVisible();
  await modal.getByRole('button', { name: 'Добавить', exact: true }).click();
  await expect(toast(authorPage, 'Добавлено документов: 1. Они появятся в ответах после обработки')).toBeVisible();
  await authorPage.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
  const row = authorPage.getByRole('row').filter({ hasText: pdf.name });
  await expect(row).toContainText('Готов', { timeout: 45_000 });
  await expect(row).toContainText('Вы');
  await expect(row.getByRole('cell', { name: '2', exact: true })).toBeVisible();

  // Другой сотрудник видит документ в общей базе и получает ответ со ссылкой на него.
  const page = await pageAs(reader, '/chat');
  await chooseKnowledge(page, 'Общая');
  await send(page, `${mark}b`);
  await expectAnswerReady(page);

  await expect(page.getByRole('heading', { name: 'Источники' })).toBeVisible();
  const card = page.getByRole('button', { name: new RegExp(`^1\\s*${pdf.name}`) });
  await expect(card).toContainText('стр. 2');
  await expect(card).toContainText(`Second page says the lease term ${mark}b is forty nine years.`);
  await expect(page.getByRole('button', { name: `Источник 1: ${pdf.name}, страница 2` }).last()).toBeVisible();

  // Карточка открывает документ на нужной странице с подсвеченной цитатой.
  await card.click();
  const viewer = page.getByRole('dialog');
  await expect(viewer).toContainText(pdf.name);
  await expect(viewer).toContainText('Общая база');
  await expect(viewer).toContainText('Страница 2 из 2');
  await expect(viewer.locator('mark')).toContainText(`${mark}b`);
  await expect(viewer.getByRole('link', { name: 'Открыть оригинал' })).toHaveAttribute(
    'href',
    /\/api\/kb\/documents\/[0-9a-f-]+\/file#page=2$/,
  );
  await viewer.getByRole('button', { name: 'Предыдущая страница' }).click();
  await expect(viewer).toContainText('Страница 1 из 2');
  await expect(viewer).toContainText(`${mark}a`);
});

test('личный документ не находится ни у другого сотрудника, ни у администратора; владелец его находит', async ({
  newMember,
  pageAs,
}) => {
  const owner = await newMember();
  const colleague = await newMember();
  const admin = await newMember('admin');
  const mark = token();
  const file = textFile(`личное-${mark}.txt`, `Личная заметка: кодовое слово ${mark} и срок сдачи отчёта.`);
  const document = await addKbDocument(owner.api, file, { scope: 'personal' });

  for (const stranger of [colleague, admin]) {
    const { events } = await chatWithAnswer(stranger.api, mark, { knowledge: 'shared_and_personal' });
    const sources = eventsOf(events, 'sources').flatMap((event) => event.data.sources);
    expect(sources.map((source: { document_id: string }) => source.document_id)).not.toContain(document.id);
    expect(JSON.stringify(events)).not.toContain(`кодовое слово ${mark}`);

    const personal = await (await stranger.api.get('/api/kb/documents?scope=personal')).json();
    expect(personal.items).toEqual([]);
    const shared = await (await stranger.api.get(`/api/kb/documents?scope=shared&q=${mark}`)).json();
    expect(shared.items).toEqual([]);
  }

  // Режим «Общая» личную базу владельца не трогает.
  const sharedOnly = await chatWithAnswer(owner.api, mark, { knowledge: 'shared' });
  const sharedSources = eventsOf(sharedOnly.events, 'sources').flatMap((event) => event.data.sources);
  expect(sharedSources.map((source: { document_id: string }) => source.document_id)).not.toContain(document.id);

  const page = await pageAs(owner, '/chat');
  await chooseKnowledge(page, 'Общая и моя');
  await send(page, mark);
  await expectAnswerReady(page);
  const card = page.getByRole('button', { name: new RegExp(`^1\\s*${file.name}`) });
  await expect(card).toContainText('Мой документ');
  await expect(card).toContainText(`кодовое слово ${mark}`);

  // У администратора вкладка «Моя» пуста: чужие личные документы в неё не попадают.
  const adminPage = await pageAs(admin, '/knowledge');
  await adminPage.getByText('Моя', { exact: true }).click();
  await expect(adminPage.getByRole('heading', { name: 'Здесь будут ваши личные документы' })).toBeVisible();
  await expect(adminPage.getByText('Их видите только вы. Администратор и коллеги доступа к ним не имеют')).toBeVisible();
});

test('скан без текстового слоя индексируется и находится поиском (распознавание — заглушка)', async ({
  newMember,
  pageAs,
}) => {
  const member = await newMember();
  const mark = uniqueSuffix();
  const scan = pngImage(`скан-${mark}.png`, mark);
  const document = await addKbDocument(member.api, scan, { scope: 'personal' });

  const text = await (await member.api.get(`/api/kb/documents/${document.id}/text?page=1`)).json();
  expect(text.recognized).toBe(true);
  const recognized: string = text.segments.map((segment: { text: string }) => segment.text).join('');
  expect(recognized).toMatch(/^Текст страницы-заглушки [0-9a-f]{8}\.$/);
  const digest = must(/([0-9a-f]{8})\.$/.exec(recognized)?.[1], 'метка распознанной страницы');

  const { events } = await chatWithAnswer(member.api, digest, { knowledge: 'shared_and_personal' });
  const sources = eventsOf(events, 'sources').flatMap((event) => event.data.sources);
  expect(sources[0]).toMatchObject({ n: 1, document_id: document.id, page: 1, quote: recognized });

  // В просмотре распознанный текст помечен как возможный источник неточностей.
  const page = await pageAs(member, '/knowledge');
  await page.getByText('Моя', { exact: true }).click();
  await page.getByRole('button', { name: scan.name, exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText(
    'Текст распознан автоматически — возможны неточности. Сверяйте с оригиналом',
  );
});

test('помощник CoGIS отвечает со ссылками на документацию CoGIS и не берёт обычные документы', async ({
  newMember,
  pageAs,
}) => {
  const author = await newMember();
  const reader = await newMember();
  const mark = token();
  const manual = textFile(
    `cogis-sdk-${mark}.md`,
    `# Плагины CoGIS\n\nИнтерфейс IPlugin${mark} описывает точку входа плагина и метод Initialize.`,
  );
  const ordinary = textFile(`приказ-${mark}.txt`, `Приказ ${mark}x о порядке согласования IPlugin${mark}.`);
  await addKbDocument(author.api, manual, { scope: 'shared', cogis: true });
  const ordinaryDocument = await addKbDocument(author.api, ordinary, { scope: 'shared' });

  const available = await (await reader.api.get('/api/kb/cogis-documentation')).json();
  expect(available).toEqual({ available: true });

  const page = await pageAs(reader, '/cogis');
  await expect(page.getByText('В базе знаний нет документации CoGIS')).toHaveCount(0);
  await send(page, `IPlugin${mark}`, 'Опишите, что должен делать плагин');
  await expectAnswerReady(page);

  // Всё, что помощник передал модели, — только документация CoGIS.
  const dialogId = await createDialog(reader.api, 'cogis');
  const stream = await reader.api.post(`/api/dialogs/${dialogId}/messages`, {
    data: { content: `IPlugin${mark}`, action: 'write' },
  });
  const events = parseStream(await stream.text());
  const sources = eventsOf(events, 'sources').flatMap((event) => event.data.sources);
  expect(sources[0]).toMatchObject({ n: 1, document_title: manual.name, page: null });
  expect(sources.map((source: { document_id: string }) => source.document_id)).not.toContain(ordinaryDocument.id);

  await expect(page.getByRole('heading', { name: 'Источники' })).toBeVisible();
  const card = page.getByRole('button', { name: new RegExp(`^1\\s*${manual.name}`) });
  await expect(card).toContainText(`Интерфейс IPlugin${mark} описывает точку входа плагина`);
  // У документа без страниц страница в источнике не показывается.
  await expect(card).not.toContainText('стр.');

  // Вопрос остаётся в истории помощника.
  await page.reload();
  await expect(page.getByRole('complementary', { name: 'История' }).getByRole('link')).toHaveCount(2);
});

test('удаление: чужой общий документ сотрудник удалить не может, автор — может; карточка источника остаётся', async ({
  newMember,
  pageAs,
}) => {
  const author = await newMember();
  const colleague = await newMember();
  const mark = token();
  const file = textFile(`инструкция-${mark}.txt`, `Инструкция ${mark} по оформлению договора аренды участка.`);
  const document = await addKbDocument(author.api, file, { scope: 'shared' });
  const { dialogId } = await chatWithAnswer(colleague.api, mark, { knowledge: 'shared' });

  await expectError(await colleague.api.delete(`/api/kb/documents/${document.id}`), 403, 'forbidden');
  await expectError(await colleague.api.post(`/api/kb/documents/${document.id}/retry`), 403, 'forbidden');
  const colleaguePage = await pageAs(colleague, '/knowledge');
  await colleaguePage.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
  await expect(colleaguePage.getByRole('row').filter({ hasText: file.name })).toBeVisible();
  await expect(colleaguePage.getByRole('button', { name: `Действия: ${file.name}` })).toHaveCount(0);

  const page = await pageAs(author, '/knowledge');
  await page.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
  await page.getByRole('button', { name: `Действия: ${file.name}` }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Удалить' }).click();
  await expect(page.getByText(`Удалить документ „${file.name}“ из общей базы?`)).toBeVisible();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить' }).click();
  await expect(toast(page, 'Документ удалён')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Ничего не нашлось' })).toBeVisible();

  await expectError(await colleague.api.get(`/api/kb/documents/${document.id}`), 404, 'not_found');
  await expectError(await colleague.api.get(`/api/kb/documents/${document.id}/file`), 404, 'not_found');
  // Удалённый документ сразу пропадает из новых ответов.
  const after = await chatWithAnswer(colleague.api, mark, { knowledge: 'shared' });
  const sources = eventsOf(after.events, 'sources').flatMap((event) => event.data.sources);
  expect(sources.map((source: { document_id: string }) => source.document_id)).not.toContain(document.id);

  // Карточка в прежнем ответе осталась, но документ по ней больше не открывается.
  await colleaguePage.goto(`/chat/${dialogId}`);
  const card = colleaguePage.getByRole('button', { name: new RegExp(`^1\\s*${file.name}`) });
  await expect(card).toContainText(`Инструкция ${mark}`);
  await card.click();
  await expect(toast(colleaguePage, 'Документ удалён из базы знаний')).toBeVisible();
});

test('администратор удаляет чужой документ общей базы', async ({ newMember, pageAs }) => {
  const author = await newMember();
  const admin = await newMember('admin');
  const mark = token();
  const file = textFile(`регламент-${mark}.txt`, `Регламент ${mark} рассмотрения заявлений о предоставлении участка.`);
  const document = await addKbDocument(author.api, file, { scope: 'shared' });

  const page = await pageAs(admin, '/knowledge');
  await page.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
  await page.getByRole('button', { name: `Действия: ${file.name}` }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Удалить' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить' }).click();
  await expect(toast(page, 'Документ удалён')).toBeVisible();
  await expectError(await author.api.get(`/api/kb/documents/${document.id}`), 404, 'not_found');
});

test('повторная загрузка того же файла в ту же базу распознаётся', async ({ newMember, pageAs }) => {
  const author = await newMember();
  const colleague = await newMember();
  const mark = token();
  const file = textFile(`памятка-${mark}.txt`, `Памятка ${mark} о сроках подачи документов на регистрацию.`);
  await addKbDocument(author.api, file, { scope: 'shared' });

  const page = await pageAs(colleague, '/knowledge');
  await page.getByRole('button', { name: 'Добавить документы' }).first().click();
  const modal = page.getByRole('dialog');
  await modal.locator('input[type=file]').setInputFiles({ ...file, name: `копия-${mark}.txt` });
  await modal.getByRole('button', { name: 'Добавить', exact: true }).click();
  await expect(modal.getByRole('alert')).toContainText(`Такой документ уже есть в этой базе: „${file.name}“`);

  // В личную базу тот же файл добавить можно: сравнение идёт внутри одной базы.
  const personal = await colleague.api.post('/api/kb/documents', {
    multipart: { file, scope: 'personal', is_cogis: 'false' },
  });
  expect(personal.status()).toBe(201);
});

test('неподдерживаемый файл в базу знаний не принимается', async ({ newMember }) => {
  const member = await newMember();
  const rejected = await member.api.post('/api/kb/documents', {
    multipart: {
      // Расширение .pdf, но содержимое — не PDF: тип определяется по содержимому.
      file: { name: 'подделка.pdf', mimeType: 'application/pdf', buffer: Buffer.from('MZ\u0000\u0000исполняемый файл') },
      scope: 'shared',
      is_cogis: 'false',
    },
  });
  await expectError(rejected, 415, 'unsupported_file_type');
});

test('если в ответе нет ссылок на найденное, интерфейс так и говорит', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const mark = token();
  await addKbDocument(member.api, textFile(`справка-${mark}.txt`, `Справка ${mark} о кадастровой стоимости.`), {
    scope: 'personal',
  });
  const dialogId = await createDialog(member.api, 'chat');
  const response = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
    data: {
      content: `${mark} [[stub:reply]]В документах об этом ничего нет.[[/stub:reply]]`,
      mode: 'fast',
      knowledge: 'shared_and_personal',
    },
  });
  expect(response.status()).toBe(200);
  await response.text();

  const page = await pageAs(member, `/chat/${dialogId}`);
  await expectAnswerReady(page);
  await expect(page.getByText('В ответе нет ссылок на документы базы знаний')).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Источники' })).toHaveCount(0);
});

test('после удаления последней строки на второй странице список переходит на первую', async ({ newMember, pageAs }) => {
  test.setTimeout(120_000);
  const member = await newMember();
  const mark = token();
  // 51 документ: вторая страница личной базы состоит из одной строки — самой старой.
  const names: string[] = [];
  for (let index = 0; index < 51; index += 1) {
    const file = textFile(`док-${mark}-${String(index).padStart(2, '0')}.txt`, `Документ ${mark} номер ${index}.`);
    const uploaded = await member.api.post('/api/kb/documents', {
      multipart: { file, scope: 'personal', is_cogis: 'false' },
    });
    expect(uploaded.status(), await uploaded.text()).toBe(201);
    names.push(file.name);
  }

  const page = await pageAs(member, '/knowledge');
  await page.getByText('Моя', { exact: true }).click();
  // Сортировка по названию: порядок строк не зависит от времени загрузки.
  await page.getByRole('columnheader', { name: 'Название' }).getByRole('button').click();
  const rows = page.getByRole('row').filter({ hasText: mark });
  await expect(rows).toHaveCount(50);
  await page.locator('[data-tid~="Paging__root"]').getByText('2', { exact: true }).click();
  await expect(rows).toHaveCount(1);
  const last = must(names.at(-1), 'последний документ');
  await expect(rows).toContainText(last);

  await page.getByRole('button', { name: `Действия: ${last}` }).click();
  await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Удалить' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить' }).click();
  await expect(toast(page, 'Документ удалён')).toBeVisible();

  // Не пустое состояние и не пустая вторая страница, а первая страница с пятьюдесятью строками.
  await expect(rows).toHaveCount(50);
  await expect(rows.first()).toContainText(must(names[0], 'первый документ'));
  await expect(page.getByRole('heading', { name: 'Здесь будут ваши личные документы' })).toHaveCount(0);
});
