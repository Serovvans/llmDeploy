/**
 * Критерии 19 и 20 (docs/portal-design.md §10): коллекция «Министерства» и теги документов.
 *
 * Общая база и «Министерства» на стенде одни на все тесты, поэтому у каждого теста свои
 * слова-метки и свои теги.
 */
import { uniqueSuffix } from '../support/accounts';
import { textFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import {
  addKbDocument,
  chatWithAnswer,
  expectError,
  kbUploadForm,
  sourcesOf,
  stubReply,
  type Scope,
  type Source,
} from '../support/portal';
import { chooseKnowledge, expectAnswerReady, tagSuggestion, send, toast } from '../support/ui';

/** Ответ заглушки со сносками на все места, которые поиск мог передать модели (`kb.search.top_k`). */
const CITE_ALL = stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]');

function token(): string {
  return `zq${uniqueSuffix()}`;
}

/** Идентификаторы документов, которые поиск нашёл по вопросу. */
async function foundDocuments(
  member: Member,
  question: string,
  options: { collections: Scope[]; tags?: string[] },
): Promise<string[]> {
  const { events } = await chatWithAnswer(member.api, `${question} ${CITE_ALL}`, options);
  return sourcesOf(events).map((source: Source) => source.document_id);
}

test.describe('«Министерства»', () => {
  test('администратор добавляет документ, сотрудник находит его в ответе только при выбранной коллекции', async ({
    newMember,
    pageAs,
  }) => {
    const admin = await newMember('admin');
    const employee = await newMember();
    const mark = token();
    const file = textFile(`приказ-минстроя-${mark}.txt`, `Приказ министерства ${mark}: срок согласования десять дней.`);

    // Администратору коллекция предлагается при загрузке.
    const adminPage = await pageAs(admin, '/knowledge');
    await adminPage.getByText('Министерства', { exact: true }).click();
    await adminPage.getByRole('button', { name: 'Добавить документы' }).first().click();
    const modal = adminPage.getByRole('dialog');
    await expect(modal.getByRole('radio', { name: /^В «Министерства»/ })).toBeChecked();
    await modal.locator('input[type=file]').setInputFiles(file);
    await modal.getByRole('button', { name: 'Добавить', exact: true }).click();
    await expect(toast(adminPage, 'Добавлено документов: 1')).toBeVisible();
    await adminPage.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
    await expect(adminPage.getByRole('row').filter({ hasText: file.name })).toContainText('Готов', { timeout: 45_000 });

    // Сотруднику вкладка доступна на чтение: документ виден, действий над ним нет.
    const page = await pageAs(employee, '/knowledge');
    await page.getByText('Министерства', { exact: true }).click();
    await expect(page.getByText('Документы этой базы добавляют администраторы.')).toBeVisible();
    await page.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
    const row = page.getByRole('row').filter({ hasText: file.name });
    await expect(row).toContainText('Готов');
    await expect(row.getByRole('button', { name: `Действия: ${file.name}` })).toHaveCount(0);
    await expect(row.getByRole('button', { name: 'Добавить теги' })).toHaveCount(0);

    // При загрузке «Министерства» сотруднику не предлагаются.
    await page.getByRole('button', { name: 'Добавить документы' }).first().click();
    const employeeModal = page.getByRole('dialog');
    await expect(employeeModal.getByRole('radio', { name: /^В общую базу/ })).toBeChecked();
    await expect(employeeModal.getByRole('radio', { name: /Министерства/ })).toHaveCount(0);
    await employeeModal.getByRole('button', { name: 'Отмена' }).click();

    // В чате документ находится при выбранной коллекции.
    await page.goto('/chat');
    await chooseKnowledge(page, ['Министерства']);
    await send(page, mark);
    await expectAnswerReady(page);
    const card = page.getByRole('button', { name: new RegExp(`^1\\s*${file.name}`) });
    await expect(card).toContainText('Министерства');
    await expect(card).toContainText(`Приказ министерства ${mark}`);

    // Когда коллекция не выбрана, документ в поиске не участвует.
    const listed = await (await employee.api.get(`/api/kb/documents?scope=ministries&q=${mark}`)).json();
    const documentId: string = listed.items[0].id;
    expect(await foundDocuments(employee, mark, { collections: ['ministries'] })).toContain(documentId);
    expect(await foundDocuments(employee, mark, { collections: ['shared', 'personal'] })).not.toContain(documentId);
  });

  test('сотрудник не может добавить, удалить, обработать заново документ «Министерств» и сменить его теги', async ({
    newMember,
  }) => {
    const admin = await newMember('admin');
    const employee = await newMember();
    const mark = token();
    const document = await addKbDocument(admin.api, textFile(`регламент-${mark}.txt`, `Регламент ведомства ${mark}.`), {
      scope: 'ministries',
      tags: ['Минстрой'],
    });

    await expectError(
      await employee.api.post('/api/kb/documents', {
        multipart: kbUploadForm(textFile(`чужое-${mark}.txt`, `Попытка ${mark}.`), { scope: 'ministries' }),
      }),
      403,
      'forbidden',
    );
    await expectError(await employee.api.delete(`/api/kb/documents/${document.id}`), 403, 'forbidden');
    await expectError(await employee.api.post(`/api/kb/documents/${document.id}/retry`), 403, 'forbidden');
    await expectError(
      await employee.api.put(`/api/kb/documents/${document.id}/tags`, { data: { tags: ['Свой'] } }),
      403,
      'forbidden',
    );

    // Документ цел, теги прежние, и сотруднику он виден без права распоряжаться.
    const seen = await (await employee.api.get(`/api/kb/documents/${document.id}`)).json();
    expect(seen).toMatchObject({ scope: 'ministries', status: 'ready', tags: ['Минстрой'], can_delete: false });
    const listed = await (await employee.api.get(`/api/kb/documents?scope=ministries&q=${mark}`)).json();
    expect(listed.items.map((item: { id: string }) => item.id)).toEqual([document.id]);

    // Другой администратор, не автор, распоряжается документом.
    const otherAdmin = await newMember('admin');
    expect((await otherAdmin.api.delete(`/api/kb/documents/${document.id}`)).status()).toBe(204);
  });
});

test.describe('Теги', () => {
  test('документ загружается с тегами; в чате источниками становятся только документы с выбранным тегом', async ({
    newMember,
  }) => {
    const member = await newMember();
    const mark = token();
    const lease = `Аренда-${mark}`;
    const tagged = await addKbDocument(member.api, textFile(`аренда-${mark}.txt`, `Договор ${mark} об аренде участка.`), {
      scope: 'personal',
      tags: [`  ${lease}  `, 'Земля'],
    });
    const other = await addKbDocument(member.api, textFile(`закупка-${mark}.txt`, `Договор ${mark} о закупке техники.`), {
      scope: 'personal',
      tags: [`Закупки-${mark}`],
    });
    // Краевые пробелы отброшены, написание сохранено.
    expect((await (await member.api.get(`/api/kb/documents/${tagged.id}`)).json()).tags).toEqual([lease, 'Земля']);

    const personal = { collections: ['personal'] as Scope[] };
    expect((await foundDocuments(member, mark, personal)).sort()).toEqual([tagged.id, other.id].sort());
    expect(await foundDocuments(member, mark, { ...personal, tags: [lease] })).toEqual([tagged.id]);
    // Регистр и краевые пробелы тега не важны.
    expect(await foundDocuments(member, mark, { ...personal, tags: [` ${lease.toUpperCase()} `] })).toEqual([tagged.id]);
    // Любой из выбранных тегов.
    expect((await foundDocuments(member, mark, { ...personal, tags: [lease, `закупки-${mark}`] })).sort()).toEqual(
      [tagged.id, other.id].sort(),
    );
    // Тег, которого нет, — пустой результат, а не ошибка.
    expect(await foundDocuments(member, mark, { ...personal, tags: [`нет-${mark}`] })).toEqual([]);

    // Смена тегов действует на поиск сразу, без повторной обработки документа.
    const changed = await member.api.put(`/api/kb/documents/${other.id}/tags`, { data: { tags: [lease.toLowerCase()] } });
    expect(changed.status(), await changed.text()).toBe(200);
    expect(await changed.json()).toMatchObject({ status: 'ready', tags: [lease.toLowerCase()] });
    expect((await foundDocuments(member, mark, { ...personal, tags: [lease] })).sort()).toEqual([tagged.id, other.id].sort());
    const cleared = await member.api.put(`/api/kb/documents/${tagged.id}/tags`, { data: { tags: [] } });
    expect(cleared.status(), await cleared.text()).toBe(200);
    expect(await foundDocuments(member, mark, { ...personal, tags: [lease] })).toEqual([other.id]);

    // Отбор списка документов по тегу.
    const listed = await (
      await member.api.get(`/api/kb/documents?scope=personal&tags=${encodeURIComponent(lease.toUpperCase())}`)
    ).json();
    expect(listed.items.map((item: { id: string }) => item.id)).toEqual([other.id]);
  });

  test('одиннадцатый тег и тег длиннее 50 символов не принимаются; повтор в другом регистре — один тег', async ({
    newMember,
  }) => {
    const member = await newMember();
    const mark = token();
    const file = textFile(`теги-${mark}.txt`, `Документ ${mark} для проверки тегов.`);
    const eleven = Array.from({ length: 11 }, (_, index) => `тег-${index}`);

    for (const [tags, code] of [
      [eleven, 'too_many'],
      [['я'.repeat(51)], 'too_long'],
    ] as const) {
      const refused = await member.api.post('/api/kb/documents', {
        multipart: kbUploadForm(file, { scope: 'personal', tags: [...tags] }),
      });
      await expectError(refused, 422, 'validation_error');
      expect((await refused.json()).error.fields).toEqual([expect.objectContaining({ field: 'tags', code })]);
    }
    // Отказ документа не создал.
    expect((await (await member.api.get('/api/kb/documents?scope=personal')).json()).items).toEqual([]);

    const document = await addKbDocument(member.api, file, {
      scope: 'personal',
      tags: ['Аренда', ' аренда ', 'я'.repeat(50), ...eleven.slice(0, 8)],
    });
    const { tags } = await (await member.api.get(`/api/kb/documents/${document.id}`)).json();
    expect(tags).toHaveLength(10);
    expect(tags.filter((tag: string) => tag.toLowerCase() === 'аренда')).toEqual(['Аренда']);

    const tooMany = await member.api.put(`/api/kb/documents/${document.id}/tags`, { data: { tags: eleven } });
    await expectError(tooMany, 422, 'validation_error');
    expect((await (await member.api.get(`/api/kb/documents/${document.id}`)).json()).tags).toEqual(tags);
  });

  test('в общей базе теги чужого документа сотрудник сменить не может, автор и администратор — могут', async ({
    newMember,
  }) => {
    const author = await newMember();
    const colleague = await newMember();
    const admin = await newMember('admin');
    const mark = token();
    const document = await addKbDocument(author.api, textFile(`общий-${mark}.txt`, `Общий документ ${mark}.`), {
      scope: 'shared',
      tags: [`исходный-${mark}`],
    });
    const address = `/api/kb/documents/${document.id}/tags`;

    await expectError(await colleague.api.put(address, { data: { tags: ['чужой'] } }), 403, 'forbidden');
    expect((await (await colleague.api.get(`/api/kb/documents/${document.id}`)).json()).tags).toEqual([`исходный-${mark}`]);

    const byAuthor = await author.api.put(address, { data: { tags: [`автор-${mark}`] } });
    expect(byAuthor.status(), await byAuthor.text()).toBe(200);
    const byAdmin = await admin.api.put(address, { data: { tags: [`админ-${mark}`] } });
    expect(byAdmin.status(), await byAdmin.text()).toBe(200);
    expect((await byAdmin.json()).tags).toEqual([`админ-${mark}`]);
  });

  test('подсказки не раскрывают теги чужих личных документов — ни сотруднику, ни администратору', async ({
    newMember,
  }) => {
    const owner = await newMember();
    const colleague = await newMember();
    const admin = await newMember('admin');
    const mark = token();
    const secret = `секрет-${mark}`;
    const common = `общий-${mark}`;
    const personal = await addKbDocument(owner.api, textFile(`личное-${mark}.txt`, `Личная заметка ${mark}.`), {
      scope: 'personal',
      tags: [secret],
    });
    await addKbDocument(owner.api, textFile(`общее-${mark}.txt`, `Общая заметка ${mark}.`), { scope: 'shared', tags: [common] });

    const suggestions = async (member: Member, query = '') =>
      (await (await member.api.get(`/api/kb/tags?q=${encodeURIComponent(mark)}${query}`)).json()).items as string[];

    expect(await suggestions(owner)).toEqual([common, secret].sort());
    for (const stranger of [colleague, admin]) {
      expect(await suggestions(stranger)).toEqual([common]);
      expect(await suggestions(stranger, '&scope=personal')).toEqual([]);
      // Отбор по чужому тегу — пустой результат без ошибки, а сам документ и его теги недоступны.
      const listed = await stranger.api.get(`/api/kb/documents?scope=personal&tags=${encodeURIComponent(secret)}`);
      expect(listed.status()).toBe(200);
      expect((await listed.json()).items).toEqual([]);
      await expectError(await stranger.api.put(`/api/kb/documents/${personal.id}/tags`, { data: { tags: [] } }), 404, 'not_found');
      expect(await foundDocuments(stranger, mark, { collections: ['shared', 'personal'], tags: [secret] })).toEqual([]);
    }
  });

  test('на экране базы знаний документ получает теги при загрузке, список отбирается по тегу', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const mark = token();
    const tag = `Кадастр-${mark}`;
    const tagged = textFile(`с-тегом-${mark}.txt`, `Документ ${mark} с тегом.`);
    const plain = textFile(`без-тега-${mark}.txt`, `Документ ${mark} без тега.`);
    await addKbDocument(member.api, plain, { scope: 'personal' });

    const page = await pageAs(member, '/knowledge');
    await page.getByText('Моя', { exact: true }).click();
    await page.getByRole('button', { name: 'Добавить документы' }).first().click();
    const modal = page.getByRole('dialog');
    await modal.locator('input[type=file]').setInputFiles(tagged);
    // Тег добавляется из раскрывшегося списка: Enter до его появления библиотека не принимает.
    await modal.getByRole('textbox', { name: 'Теги' }).fill(tag);
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: `Добавить «${tag}»` }).click();
    await modal.getByRole('button', { name: 'Добавить', exact: true }).click();
    await expect(toast(page, 'Добавлено документов: 1')).toBeVisible();

    const taggedRow = page.getByRole('row').filter({ hasText: tagged.name });
    await expect(taggedRow).toContainText('Готов', { timeout: 45_000 });
    await expect(taggedRow).toContainText(tag);
    await expect(page.getByRole('row').filter({ hasText: plain.name })).toBeVisible();

    // Отбор по тегу из подсказок оставляет только документ с этим тегом.
    await page.getByRole('main').getByRole('textbox', { name: 'Теги' }).fill(tag.slice(0, 12));
    await tagSuggestion(page, tag).click();
    await expect(taggedRow).toBeVisible();
    await expect(page.getByRole('row').filter({ hasText: plain.name })).toHaveCount(0);

    // В чате тег выбирается из подсказок и уходит на сервер вместе с вопросом.
    await page.goto('/chat');
    await chooseKnowledge(page, ['Моя'], [tag]);
    await expect(page.getByRole('button', { name: 'База знаний: Моя, теги: 1' })).toBeVisible();
    const request = page.waitForRequest((candidate) => /\/api\/dialogs\/[^/]+\/messages$/.test(candidate.url()));
    await send(page, `${mark} ${CITE_ALL}`);
    expect((await request).postDataJSON()).toMatchObject({ collections: ['personal'], tags: [tag] });
    await expectAnswerReady(page);
    await expect(page.getByRole('button', { name: new RegExp(`^1\\s*${tagged.name}`) })).toBeVisible();
    await expect(page.getByRole('button', { name: new RegExp(`^\\d\\s*${plain.name}`) })).toHaveCount(0);
  });
});
