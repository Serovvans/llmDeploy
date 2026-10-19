/**
 * Критерий 26 (docs/portal-design.md §10, изменение 0.6) и отказы по пределам DOCX (критерий 27):
 * картинки VML, пороги размера, правила описания картинки. Заглушка модели на стенде отвечает на
 * особые ширины PNG (docs/portal-api.md §12.2): 701 — пустое описание, 702 — длинное (больше
 * 3000 символов), 703 — при `max_tokens < 4096` обрезанное начало с `finish_reason: "length"`.
 *
 * Тяжёлые проверки пределов (файл 200 МБ, 3000 страниц, 10 млн символов, 300 картинок, уступка
 * места) — в kb-limits-stand.e2e.ts: они идут один раз, проектом `stand`.
 */
import { uniqueSuffix } from '../support/accounts';
import { solidPng, wordDocument, type TestFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { addKbDocument, chatWithAnswer, createDialog, expectError, sourcesOf, stubReply, type Source } from '../support/portal';
import { standSql, workerLog } from '../support/stand';
import { chooseKnowledge, expectAnswerReady, send } from '../support/ui';

const CITE_ALL = stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]');
const MB = 1024 * 1024;
const DOCX_TOO_LARGE = 'В документе Word слишком много содержимого помимо картинок. Разделите его на части.';
const DOCX_IMAGES_TOO_LARGE =
  'Картинки в документе Word занимают слишком много места. Уменьшите их или разделите документ на части.';

/** Число строк длинного описания заглушки: строки добавляются, пока текст не длиннее 3000 символов. */
const LONG_DESCRIPTION_LINES = (() => {
  let text = '';
  let lines = 0;
  while (text.length <= 3000) {
    lines += 1;
    const line = `Описание изображения-заглушки 01234567, строка ${lines}.`;
    text = text ? `${text}\n${line}` : line;
  }
  return lines;
})();

function token(): string {
  return `zq${uniqueSuffix()}`;
}

/** Цвет картинки: свой у каждого теста и номера, чтобы картинки не совпали по хешу. */
function color(mark: string, index: number): [number, number, number] {
  const base = Number.parseInt(mark.slice(-4), 36) % 200;
  return [base, index % 256, (index >> 8) + 17];
}

async function personalSources(member: Member, question: string): Promise<Source[]> {
  const { events } = await chatWithAnswer(member.api, `${question} ${CITE_ALL}`, { collections: ['personal'] });
  return sourcesOf(events);
}

/** Сохранённые картинки документа по порядку: идентификатор и размер. */
function storedImages(documentId: string): { id: string; size: string }[] {
  return standSql(`SELECT id, width || 'x' || height FROM kb_images WHERE document_id = '${documentId}' ORDER BY ordinal`).map(
    (line) => {
      const [id = '', size = ''] = line.split('|');
      return { id, size };
    },
  );
}

/** Записи журнала воркера (JSON) о картинках документа. */
function imageLogEntries(since: string, documentId: string): { message: string; reason: string }[] {
  return workerLog(since)
    .split('\n')
    .filter((line) => line.includes(documentId))
    .map((line) => JSON.parse(line.slice(line.indexOf('{'))))
    .filter((entry) => entry.document_id === documentId && /^image /.test(entry.message))
    .map((entry) => ({ message: entry.message, reason: entry.reason }));
}

test.describe('Картинки из DOCX (0.6)', () => {
  test('картинка VML берётся; DrawingML с запасным VML — одна картинка; полоса 900×60 сохраняется, значок 64×64 — нет', async ({
    newMember,
  }) => {
    const member = await newMember();
    const mark = token();
    const file = wordDocument(`инструкция-${mark}.docx`, {
      paragraphs: [`Инструкция ${mark} по работе с реестром.`],
      images: [
        { png: solidPng(200, 200, color(mark, 1)), link: 'vml' },
        { png: solidPng(900, 60, color(mark, 2)), link: 'drawing' },
        { png: solidPng(64, 64, color(mark, 3)), link: 'drawing' },
        { png: solidPng(300, 150, color(mark, 4)), link: 'drawing+vml' },
      ],
    });
    const document = await addKbDocument(member.api, file, { scope: 'personal' });

    const images = storedImages(document.id);
    expect(images.map((image) => image.size)).toEqual(['200x200', '900x60', '300x150']);
    const state = await (await member.api.get(`/api/kb/documents/${document.id}`)).json();
    expect(state).toMatchObject({ status: 'ready', images_truncated: false });

    // Все три картинки находятся по описанию.
    const sources = await personalSources(member, 'Описание изображения-заглушки');
    const found = new Set(sources.filter((source) => source.document_id === document.id).map((source) => source.image_id));
    found.delete(null);
    expect([...found].sort()).toEqual(images.map((image) => image.id).sort());
  });

  test('пустое описание отбрасывает картинку с записью в журнале; описание, упёршееся в предел, запрашивается повторно; длинное даёт несколько фрагментов и одну картинку под ответом', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const mark = token();
    const since = new Date(Date.now() - 1000).toISOString();
    const file = wordDocument(`схемы-${mark}.docx`, {
      paragraphs: [`Схемы ${mark} к регламенту.`],
      images: [
        { png: solidPng(701, 60, color(mark, 1)) },
        { png: solidPng(702, 60, color(mark, 2)) },
        { png: solidPng(703, 60, color(mark, 3)) },
      ],
    });
    const document = await addKbDocument(member.api, file, { scope: 'personal' });

    // Картинка с пустым описанием удалена при замене индекса; в журнале — причина, без текста.
    const images = storedImages(document.id);
    expect(images.map((image) => image.size)).toEqual(['702x60', '703x60']);
    const [long, retried] = images.map((image) => image.id);
    const entries = imageLogEntries(since, document.id);
    expect(entries).toContainEqual({ message: 'image dropped', reason: 'empty' });
    // Повтор с большим пределом дал полное описание: обрезанным ничего не сохранено.
    expect(entries.filter((entry) => entry.message === 'image description truncated')).toEqual([]);

    const sources = (await personalSources(member, 'Описание изображения-заглушки строка')).filter(
      (source) => source.document_id === document.id,
    );
    const retriedSources = sources.filter((source) => source.image_id === retried);
    expect(retriedSources.map((source) => source.quote)).toEqual([expect.stringMatching(/^Описание изображения-заглушки [0-9a-f]{8}\.$/)]);
    expect(sources.some((source) => source.quote.includes('Начало описания-заглушки'))).toBe(false);
    const longSources = sources.filter((source) => source.image_id === long);
    expect(longSources.length).toBeGreaterThan(1);
    for (const source of longSources) {
      expect(source.quote).toMatch(/^Описание изображения-заглушки [0-9a-f]{8}, строка \d+\./m);
      expect(source.quote.length).toBeLessThanOrEqual(1200);
    }
    // Части покрывают описание целиком: есть и первая, и последняя строка.
    const quotes = longSources.map((source) => source.quote).join('\n');
    expect(quotes).toContain(', строка 1.');
    expect(quotes).toContain(`, строка ${LONG_DESCRIPTION_LINES}.`);

    // Под ответом каждая картинка — один раз, сколько бы её частей ни попало в источники.
    const page = await pageAs(member, '/chat');
    await chooseKnowledge(page, ['Моя']);
    await send(page, `Описание изображения-заглушки строка ${CITE_ALL}`);
    await expectAnswerReady(page);
    const figures = page.getByRole('button', { name: new RegExp(`^Картинка из документа ${file.name}, источник \\d+$`) });
    await expect(figures).toHaveCount(2);
  });
});

test.describe('Отказы по пределам DOCX (0.6)', () => {
  /** DOCX, где помимо картинок распаковывается больше 64 МБ: вложенный объект из нулей. */
  function tooMuchContent(mark: string): TestFile {
    return wordDocument(`отчёт-${mark}.docx`, {
      paragraphs: [`Отчёт ${mark}.`],
      extraParts: [{ path: 'word/embeddings/oleObject1.bin', data: Buffer.alloc(64 * MB + 1), deflate: true }],
    });
  }

  /** DOCX, у которого картинки после распаковки больше 256 МБ; файл сжат до сотен КБ. */
  function tooManyImages(mark: string): TestFile {
    return wordDocument(`альбом-${mark}.docx`, {
      paragraphs: [`Альбом ${mark}.`],
      extraParts: [{ path: 'word/media/image99.png', data: Buffer.alloc(256 * MB + 1), deflate: true }],
    });
  }

  test('база знаний, вложение чата и разбор отвечают docx_too_large и docx_images_too_large с понятным текстом', async ({
    newMember,
  }) => {
    const member = await newMember();
    const mark = token();
    const dialogId = await createDialog(member.api, 'chat');
    const cases: [TestFile, string, string][] = [
      [tooMuchContent(mark), 'docx_too_large', DOCX_TOO_LARGE],
      [tooManyImages(mark), 'docx_images_too_large', DOCX_IMAGES_TOO_LARGE],
    ];
    for (const [file, code, message] of cases) {
      expect(file.buffer.length).toBeLessThan(MB);
      const routes = [
        member.api.post('/api/kb/documents', { multipart: { file, scope: 'personal', is_cogis: 'false' } }),
        member.api.post(`/api/dialogs/${dialogId}/attachments`, { multipart: { file } }),
        member.api.post('/api/docparse', { multipart: { file, template_id: 'egrn' } }),
      ];
      for (const response of await Promise.all(routes)) {
        await expectError(response, 422, code);
        expect((await response.json()).error.message).toBe(message);
      }
    }
    expect((await (await member.api.get('/api/kb/documents?scope=personal')).json()).items).toEqual([]);
  });

  test('окно добавления в базу знаний называет новые пределы и объясняет отказ по каждому DOCX', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const mark = token();
    const page = await pageAs(member, '/knowledge');
    await page.getByRole('button', { name: 'Добавить документы' }).first().click();
    const modal = page.getByRole('dialog');
    await expect(modal).toContainText('до 200 МБ каждый, PDF до 3000 страниц');
    await modal.locator('input[type=file]').setInputFiles([tooMuchContent(mark), tooManyImages(mark)]);
    await modal.getByRole('button', { name: 'Добавить', exact: true }).click();
    const alerts = modal.getByRole('alert');
    await expect(alerts.filter({ hasText: 'В документе слишком много содержимого помимо картинок. Разделите его на части' })).toHaveCount(1);
    await expect(
      alerts.filter({ hasText: 'Картинки в документе занимают слишком много места. Уменьшите их или разделите документ на части' }),
    ).toHaveCount(1);
  });
});
