/**
 * Критерии 26–28 (docs/portal-design.md §10, изменение 0.6), которые надолго занимают воркер или
 * меняют состояние стенда: документы на пределах размера, 300 картинок, уступка места длинным
 * документом, переобработка `--images`. Идут один раз, проектом `stand` (playwright.config.ts).
 *
 * Тяжёлые документы тест удаляет в конце: иначе каждая следующая массовая переобработка на стенде
 * проходила бы и по ним.
 */
import { uniqueSuffix } from '../support/accounts';
import { noisePng, paddedPdf, solidPng, textFile, textPdf, unzip, wordDocument, type TestFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import {
  addKbDocument,
  chatWithAnswer,
  createDialog,
  expectError,
  kbUploadForm,
  sourcesOf,
  stubReply,
  waitKbReady,
  type KbDocument,
  type Source,
} from '../support/portal';
import { restartWorkerWith, standSql, stubCompletions, workerCli, workerLog } from '../support/stand';

const CITE_ALL = stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]');
const MB = 1024 * 1024;
const MAX_DOCUMENT_BYTES = 200 * MB;
const MAX_PAGES = 3000;
const MAX_CHARS = 10_000_000;
const MAX_IMAGES = 300;

function token(): string {
  return `zq${uniqueSuffix()}`;
}

async function documentState(member: Member, documentId: string) {
  return (await member.api.get(`/api/kb/documents/${documentId}`)).json();
}

async function upload(member: Member, file: TestFile) {
  return member.api.post('/api/kb/documents', { multipart: kbUploadForm(file, { scope: 'personal' }), timeout: 300_000 });
}

/** Загружает документ, не дожидаясь обработки. */
async function enqueue(member: Member, file: TestFile): Promise<KbDocument> {
  const response = await upload(member, file);
  expect(response.status(), await response.text()).toBe(201);
  return response.json();
}

async function remove(member: Member, ...documents: KbDocument[]): Promise<void> {
  for (const document of documents) {
    expect((await member.api.delete(`/api/kb/documents/${document.id}`)).status()).toBe(204);
  }
}

async function documentSources(member: Member, question: string, documentId: string): Promise<Source[]> {
  const { events } = await chatWithAnswer(member.api, `${question} ${CITE_ALL}`, { collections: ['personal'] });
  return sourcesOf(events).filter((source) => source.document_id === documentId);
}

function imageCount(documentId: string): number {
  return Number(standSql(`SELECT count(*) FROM kb_images WHERE document_id = '${documentId}'`)[0]);
}

/**
 * DOCX с `count` разными одноцветными картинками 150×150 — каждая проходит пороги (§8.6); у
 * последней ширина `lastWidth`, чтобы было видно, сохранена ли она.
 */
function picturesDocx(name: string, mark: string, count: number, lastWidth = 150): TestFile {
  const seed = Number.parseInt(mark.slice(-3), 36) % 250;
  return wordDocument(name, {
    paragraphs: [`Альбом ${mark} снимков экрана.`],
    images: Array.from({ length: count }, (_, index) => ({
      png: solidPng(index === count - 1 ? lastWidth : 150, 150, [seed, index % 256, Math.floor(index / 256) + 1]),
    })),
  });
}

/** Текст ровно из `length` символов: предложения с номерами, метка `mark` — в середине. */
function longText(mark: string, length: number): string {
  const sentence = 'Порядок учёта земельных участков описан в разделе регламента. ';
  const middle = Math.floor(length / 2);
  const marked = `Особая отметка ${mark} о кадастровом номере. `;
  const body = sentence.repeat(Math.ceil(length / sentence.length) + 1);
  const text = `${body.slice(0, middle)}${marked}${body.slice(middle)}`.slice(0, length - 1);
  return `${text}.`;
}

/** Опрашивает `portal worker-health`, пока идёт `work`: отметка «воркер жив» не должна устареть. */
async function whileWorkerAlive<T>(work: Promise<T>): Promise<T> {
  let done = false;
  const watch = (async () => {
    while (!done) {
      workerCli('worker-health');
      await new Promise((resolve) => setTimeout(resolve, 2_000));
    }
  })();
  try {
    return await work;
  } finally {
    done = true;
    await watch;
  }
}

test.describe.configure({ mode: 'serial' });

test('документ с 300 картинками сохраняет все, с 301 — первые 300 и отметку «обработаны не все» @стенд', async ({ newMember }) => {
  test.setTimeout(600_000);
  const member = await newMember();
  const mark = token();
  const full = await enqueue(member, picturesDocx(`альбом-${mark}.docx`, mark, MAX_IMAGES));
  const over = await enqueue(member, picturesDocx(`альбом-больше-${mark}.docx`, mark, MAX_IMAGES + 1, 160));
  await waitKbReady(member.api, full.id, 300_000);
  await waitKbReady(member.api, over.id, 300_000);

  expect(await documentState(member, full.id)).toMatchObject({ images_truncated: false });
  expect(imageCount(full.id)).toBe(MAX_IMAGES);
  expect(await documentState(member, over.id)).toMatchObject({ images_truncated: true });
  expect(imageCount(over.id)).toBe(MAX_IMAGES);
  // Взяты первые по порядку: последняя картинка документа — шире остальных — не сохранена.
  expect(standSql(`SELECT DISTINCT width FROM kb_images WHERE document_id = '${over.id}'`)).toEqual(['150']);
  await remove(member, full, over);
});

test('файл 200 МБ через Caddy принимается и обрабатывается; на байт больше — понятный отказ; вложение чата больше 64 МБ отклоняется @стенд', async ({
  newMember,
}) => {
  test.setTimeout(600_000);
  const member = await newMember();
  const mark = token();
  const largest = await enqueue(member, paddedPdf(`атлас-${mark}.pdf`, `Atlas ${mark} of parcels.`, MAX_DOCUMENT_BYTES));
  expect(largest).toMatchObject({ page_count: 1 });

  const tooLarge = await upload(member, paddedPdf(`атлас-2-${mark}.pdf`, `Atlas ${mark} second.`, MAX_DOCUMENT_BYTES + 1));
  await expectError(tooLarge, 413, 'file_too_large');
  expect((await tooLarge.json()).error.details).toEqual({ max_bytes: MAX_DOCUMENT_BYTES });

  // Предел вложения чата прежний; отвечает бэкенд — его предел 20 МБ меньше предела Caddy (64 МБ).
  const dialogId = await createDialog(member.api, 'chat');
  const attachment = await member.api.post(`/api/dialogs/${dialogId}/attachments`, {
    multipart: { file: textFile(`журнал-${mark}.txt`, 'a'.repeat(65 * MB)) },
  });
  expect(attachment.status()).toBe(413);

  await waitKbReady(member.api, largest.id, 300_000);
  expect((await documentSources(member, mark, largest.id)).length).toBeGreaterThan(0);
  await remove(member, largest);
});

test('PDF в 3000 страниц принимается и находится поиском; 3001 страница — отказ с пределом @стенд', async ({ newMember }) => {
  test.setTimeout(600_000);
  const member = await newMember();
  const mark = token();
  const pages = (count: number) => Array.from({ length: count }, (_, index) => `Sheet ${index + 1} of register ${mark}.`);
  const document = await enqueue(member, textPdf(`реестр-${mark}.pdf`, pages(MAX_PAGES)));
  expect(document).toMatchObject({ page_count: MAX_PAGES });

  const refused = await upload(member, textPdf(`реестр-2-${mark}.pdf`, pages(MAX_PAGES + 1)));
  await expectError(refused, 422, 'too_many_pages');
  expect((await refused.json()).error.details).toEqual({ max_pages: MAX_PAGES });

  await waitKbReady(member.api, document.id, 480_000);
  const sources = await documentSources(member, `Sheet 2999 of register ${mark}`, document.id);
  expect(sources.map((source) => source.page)).toContain(2999);
  await remove(member, document);
});

test('текст в 10 млн символов становится «готов» и находится поиском, отметка «воркер жив» не устаревает; на символ больше — document_too_long @стенд', async ({
  newMember,
}) => {
  test.setTimeout(900_000);
  const member = await newMember();
  const mark = token();
  const text = longText(mark, MAX_CHARS);
  expect(text.length).toBe(MAX_CHARS);
  const document = await enqueue(member, textFile(`свод-${mark}.txt`, text));
  const over = await enqueue(member, textFile(`свод-2-${mark}.txt`, longText(`${mark}x`, MAX_CHARS + 1)));

  await whileWorkerAlive(waitKbReady(member.api, document.id, 840_000));
  const sources = await documentSources(member, `Особая отметка ${mark}`, document.id);
  expect(sources.map((source) => source.quote)).toContainEqual(expect.stringContaining(mark));

  await expect.poll(async () => (await documentState(member, over.id)).status, { timeout: 120_000 }).toBe('error');
  expect(await documentState(member, over.id)).toMatchObject({ error_code: 'document_too_long' });
  await remove(member, document, over);
});

test('DOCX со скриншотами на 100 МБ принимается и обрабатывается со всеми картинками @стенд', async ({ newMember }) => {
  test.setTimeout(600_000);
  const member = await newMember();
  const mark = token();
  // 40 несжимаемых картинок по 2,5 МБ: картинки отдельного предела (256 МБ) не превышают.
  const file = wordDocument(`скриншоты-${mark}.docx`, {
    paragraphs: [`Инструкция ${mark} со снимками экрана.`],
    images: Array.from({ length: 40 }, (_, index) => ({ png: noisePng(1000, 834, `${mark}-${index}`) })),
  });
  expect(file.buffer.length).toBeGreaterThan(100 * 1000 * 1000);
  const document = await enqueue(member, file);
  await waitKbReady(member.api, document.id, 480_000);
  expect(imageCount(document.id)).toBe(40);
  expect(await documentState(member, document.id)).toMatchObject({ images_truncated: false });
  await remove(member, document);
});

test('DOCX с разметкой больше 16 МБ и текстом в несколько миллионов символов принимается, становится «готов» и находится поиском @стенд', async ({
  newMember,
}) => {
  test.setTimeout(900_000);
  const member = await newMember();
  const mark = token();
  const paragraph = 'Порядок учёта земельных участков описан в разделе регламента о ведении реестра.';
  const paragraphs = Array.from({ length: 40_000 }, (_, index) => `${index + 1}. ${paragraph}`);
  paragraphs.splice(20_000, 0, `Особая отметка ${mark} о кадастровом номере.`);
  const file = wordDocument(`регламент-${mark}.docx`, { paragraphs, runPerWord: true });
  const markup = must(unzip(file.buffer).get('word/document.xml'), 'разметка документа');
  expect(markup.length).toBeGreaterThan(16 * MB);
  expect(paragraphs.join('').length).toBeGreaterThan(3_000_000);

  const document = await enqueue(member, file);
  await whileWorkerAlive(waitKbReady(member.api, document.id, 840_000));
  const sources = await documentSources(member, `Особая отметка ${mark}`, document.id);
  expect(sources.map((source) => source.quote)).toContainEqual(expect.stringContaining(mark));
  await remove(member, document);
});

test('пока обрабатывается длинный документ, документ другого сотрудника, загруженный позже, становится «готов» раньше него @стенд', async ({
  newMember,
}) => {
  test.setTimeout(600_000);
  const owner = await newMember();
  const colleague = await newMember();
  const mark = token();
  const since = new Date(Date.now() - 1000).toISOString();
  // Квант на стенде — доли секунды: на рабочем значении (300 с) длинный документ стенда не успевает его исчерпать.
  const restore = restartWorkerWith('kb:\n  worker:\n    slice_seconds: 0.2\n');
  try {
    // Короткий документ загружается сразу за длинным: воркер берёт задания по порядку, первым — длинный,
    // и короткий без уступки ждал бы его конца.
    const long = await enqueue(owner, picturesDocx(`длинный-${mark}.docx`, mark, MAX_IMAGES));
    const short = await enqueue(colleague, textFile(`памятка-${mark}.txt`, `Памятка ${mark} о сроках подачи заявлений.`));

    let longStatusWhenShortReady = '';
    await expect
      .poll(
        async () => {
          const status = (await documentState(colleague, short.id)).status;
          longStatusWhenShortReady = (await documentState(owner, long.id)).status;
          return status;
        },
        { timeout: 120_000, intervals: [200] },
      )
      .toBe('ready');
    expect(['queued', 'processing']).toContain(longStatusWhenShortReady);

    // Длинный документ продолжает с места остановки: картинки не теряются и не повторяются.
    await whileWorkerAlive(waitKbReady(owner.api, long.id, 300_000));
    expect(imageCount(long.id)).toBe(MAX_IMAGES);
    const yielded = workerLog(since)
      .split('\n')
      .filter((line) => line.includes('"job yielded"'));
    expect(yielded.length).toBeGreaterThan(0);
    await remove(owner, long);
  } finally {
    restore();
  }
});

test('переобработка --images берёт PDF, DOCX и картинки, но не TXT; документ при ней находится поиском в прежнем виде; пояснения переносятся без обращения к модели @стенд', async ({
  newMember,
}) => {
  test.setTimeout(600_000);
  const member = await newMember();
  const mark = token();
  const text = await addKbDocument(member.api, textFile(`памятка-${mark}.txt`, `Памятка ${mark} о сроках.`), { scope: 'personal' });
  const pdf = await addKbDocument(
    member.api,
    textPdf(`plan-${mark}.pdf`, [`Site plan ${mark} of the parcel.`, `Boundary ${mark} of the parcel.`, `Owner ${mark} of the parcel.`]),
    { scope: 'personal' },
  );
  const docx = await addKbDocument(member.api, picturesDocx(`схема-${mark}.docx`, mark, 1), { scope: 'personal' });
  const picture: TestFile = { name: `скан-${mark}.png`, mimeType: 'image/png', buffer: solidPng(200, 200, [1, 2, 3]) };
  const png = await addKbDocument(member.api, picture, { scope: 'personal' });
  const contexts = () =>
    standSql(`SELECT ordinal || ':' || coalesce(context, '-') FROM kb_fragments WHERE document_id = '${pdf.id}' ORDER BY ordinal`);
  // Пояснения к фрагментам PDF дописаны (задание context новой загрузки).
  await expect.poll(() => contexts().some((line) => line.endsWith(':-')), { timeout: 120_000 }).toBe(false);
  const contextsBefore = contexts();
  expect(contextsBefore).toHaveLength(3);
  const before = must((await documentSources(member, `Site plan ${mark}`, pdf.id))[0], 'источник PDF до переобработки');
  const reprocessJobs = (document: KbDocument) =>
    Number(standSql(`SELECT count(*) FROM kb_jobs WHERE document_id = '${document.id}' AND kind = 'reprocess'`)[0]);

  workerCli('reprocess', 'pause');
  try {
    expect(workerCli('reprocess', 'start', '--images')).toMatch(/поставлено на переобработку: [1-9]\d*/);
    expect(workerCli('reprocess', 'status')).toMatch(/Картинок с описанием прежней версии: \d+/);
    expect([pdf, docx, png, text].map(reprocessJobs)).toEqual([1, 1, 1, 0]);
    // Пока переобработка ждёт, документ ищется по прежнему фрагменту.
    expect((await documentSources(member, `Site plan ${mark}`, pdf.id))[0]).toMatchObject({
      fragment_id: before.fragment_id,
      quote: before.quote,
    });

    // Задание PDF поднимается выше паузы — так retry поднимает стоящее задание массовой переобработки
    // (§11.6). Перед этим воркер доделывает задания вне паузы: тогда все обращения к модели в окне
    // ниже — от переобработки этого PDF и его задания пояснений.
    await expect
      .poll(() => standSql('SELECT count(*) FROM kb_jobs WHERE document_id IS NOT NULL AND priority < 35')[0], {
        timeout: 120_000,
      })
      .toBe('0');
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    const since = new Date().toISOString();
    standSql(`UPDATE kb_jobs SET priority = 20 WHERE document_id = '${pdf.id}' AND kind = 'reprocess'`);
    await expect
      .poll(() => standSql(`SELECT count(*) FROM kb_jobs WHERE document_id = '${pdf.id}'`)[0], { timeout: 240_000 })
      .toBe('0');
    // Фрагменты новые, пояснения прежние, и модель за них не спрашивали: картинок в PDF нет.
    expect(standSql(`SELECT count(*) FROM kb_fragments WHERE id = '${before.fragment_id}'`)).toEqual(['0']);
    expect(contexts()).toEqual(contextsBefore);
    expect(stubCompletions(since)).toBe(0);
    expect((await documentSources(member, `Site plan ${mark}`, pdf.id))[0]).toMatchObject({ quote: before.quote });
  } finally {
    workerCli('reprocess', 'resume');
  }
  // --images и --all вместе не допускаются.
  expect(() => workerCli('reprocess', 'start', '--images', '--all')).toThrow();
});
