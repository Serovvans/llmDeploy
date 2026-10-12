/**
 * Проверки, которые меняют состояние всего стенда (docs/portal-design.md §10, критерии 17 и 25):
 * остановка песочницы SQL и массовая переобработка базы знаний. Идут отдельным проектом `stand`,
 * один раз и после браузерных проектов (playwright.config.ts): одновременно с ними остановленная
 * песочница сломала бы проверки SQL, а переобработка сменила бы идентификаторы фрагментов, по
 * которым другие тесты ждут подсветку цитаты.
 */
import { uniqueSuffix } from '../support/accounts';
import { pdfWithImage, textFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import {
  addKbDocument,
  askSql,
  chatWithAnswer,
  createDialog,
  eventsOf,
  expectError,
  saveSqlSchema,
  sourcesOf,
  stubReply,
  type Source,
} from '../support/portal';
import { standCompose, workerCli } from '../support/stand';

const CITE_ALL = stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]');
const TABLE = 'CREATE TABLE parcels (id int PRIMARY KEY, area numeric)';
const SCHEMA = `${TABLE};\n-- [[stub:sql-data]]${JSON.stringify({
  statements: [TABLE, 'INSERT INTO parcels VALUES (1, 120.5), (2, 80)'],
})}[[/stub:sql-data]]`;

test.describe.configure({ mode: 'serial' });

async function documentSources(member: Member, question: string, documentId: string): Promise<Source[]> {
  const { events } = await chatWithAnswer(member.api, `${question} ${CITE_ALL}`, { collections: ['personal'] });
  return sourcesOf(events).filter((source) => source.document_id === documentId);
}

async function documentState(member: Member, documentId: string) {
  return (await member.api.get(`/api/kb/documents/${documentId}`)).json();
}

/** Статус проверки единственного блока ответа. */
async function runStatus(member: Member, schemaId: string): Promise<string> {
  const dialogId = await createDialog(member.api, 'sql');
  const reply = stubReply('```sql\nSELECT id FROM parcels WHERE area > 100;\n```');
  const events = await askSql(member.api, dialogId, `Напиши запрос ${reply}`, { schemaId, testRun: true });
  expect(events.at(-1)?.event).toBe('done');
  return must(eventsOf(events, 'sql_run').at(-1), 'событие sql_run').data.blocks[0].status;
}

test('при остановленной песочнице ответ приходит со статусом «проверка недоступна»; после запуска проверка работает без перезапуска портала @стенд', async ({
  newMember,
}) => {
  test.setTimeout(180_000);
  const member = await newMember();
  const schemaId = await saveSqlSchema(member.api, `Кадастр ${uniqueSuffix()}`, SCHEMA);
  expect(await runStatus(member, schemaId)).toBe('passed');

  standCompose('stop', 'sql-sandbox');
  try {
    const dialogId = await createDialog(member.api, 'sql');
    const reply = stubReply('Запрос:\n```sql\nSELECT id FROM parcels WHERE area > 100;\n```');
    const events = await askSql(member.api, dialogId, `Напиши запрос ${reply}`, { schemaId, testRun: true });
    // Ответ и проверка синтаксиса от песочницы не зависят.
    expect(events.at(-1)?.event).toBe('done');
    expect(eventsOf(events, 'error')).toEqual([]);
    expect(must(eventsOf(events, 'sql_check')[0], 'событие sql_check').data.blocks[0].valid).toBe(true);
    const block = must(eventsOf(events, 'sql_run').at(-1), 'событие sql_run').data.blocks[0];
    expect(block).toMatchObject({ status: 'unavailable', fixture: null, result: null, error: null });
  } finally {
    standCompose('up', '-d', '--no-build', '--no-recreate', '--wait', 'sql-sandbox');
  }
  await expect.poll(() => runStatus(member, schemaId), { timeout: 60_000, intervals: [2_000] }).toBe('passed');
});

test('документ, который переобрабатывается, и документ, чья переобработка не удалась, находятся поиском в прежнем виде @стенд', async ({
  newMember,
}) => {
  test.setTimeout(300_000);
  const member = await newMember();
  const colleague = await newMember();
  const seed = uniqueSuffix();
  const mark = `zq${seed}`;
  const text = `Регламент ${mark} о сроке аренды: сорок девять лет.`;
  const healthy = await addKbDocument(member.api, textFile(`регламент-${mark}.txt`, text), { scope: 'personal' });
  const broken = await addKbDocument(
    member.api,
    pdfWithImage(`plan-${mark}.pdf`, `Site plan ${mark} of the land parcel.`, seed),
    { scope: 'personal' },
  );
  const before = must((await documentSources(member, mark, healthy.id))[0], 'источник до переобработки');
  expect(before.quote).toBe(text);
  const brokenBefore = await documentSources(member, `${mark} Описание изображения-заглушки`, broken.id);
  expect(brokenBefore.map((source) => source.image_id !== null).sort()).toEqual([false, true]);

  // Исходный файл второго документа портится в томе: картинки при переобработке взять неоткуда.
  const storageKey = standCompose(
    'exec', '-T', 'portal-db', 'psql', '-U', 'portal', '-d', 'portal', '-At',
    '-c', `SELECT storage_key FROM kb_documents WHERE id = '${broken.id}'`,
  ).toString('utf8').trim();
  expect(storageKey).toMatch(/^kb\/[0-9a-f-]{36}$/);
  standCompose('exec', '-T', 'portal-worker', 'sh', '-c', `echo повреждён > /data/files/${storageKey}`);

  workerCli('reprocess', 'pause');
  try {
    expect(workerCli('reprocess', 'start', '--all')).toMatch(/поставлено на переобработку: [1-9]\d*/);
    // Задания стоят в очереди: документы готовы и ищутся по прежним фрагментам.
    for (const document of [healthy, broken]) {
      expect(await documentState(member, document.id)).toMatchObject({ status: 'ready', reprocess_error: null });
    }
    expect((await documentSources(member, mark, healthy.id))[0]).toMatchObject({
      fragment_id: before.fragment_id,
      quote: text,
    });
  } finally {
    workerCli('reprocess', 'resume');
  }

  // Пока идёт переобработка, документ ни разу не пропадает из поиска; по её концу фрагмент новый, текст прежний.
  await expect
    .poll(
      async () => {
        const [source] = await documentSources(member, mark, healthy.id);
        expect(source?.quote, 'документ пропал из поиска во время переобработки').toBe(text);
        return source?.fragment_id;
      },
      { timeout: 240_000, intervals: [500] },
    )
    .not.toBe(before.fragment_id);
  expect(await documentState(member, healthy.id)).toMatchObject({ status: 'ready', reprocess_error: null });
  // Прежняя карточка источника открывает документ по своему месту, без подсветки.
  const opened = await member.api.get(`/api/kb/documents/${healthy.id}/text?fragment_id=${before.fragment_id}`);
  expect(opened.status()).toBe(200);
  expect((await opened.json()).segments.map((segment: { text: string }) => segment.text).join('')).toBe(text);

  // Неудачная переобработка: документ остаётся готовым, с прежними фрагментами и картинкой, и получает отметку.
  await expect
    .poll(async () => (await documentState(member, broken.id)).reprocess_error, { timeout: 240_000, intervals: [1_000] })
    .not.toBeNull();
  expect(await documentState(member, broken.id)).toMatchObject({ status: 'ready', error_code: null, reprocessing: false });
  const brokenAfter = await documentSources(member, `${mark} Описание изображения-заглушки`, broken.id);
  expect(brokenAfter.map((source) => [source.fragment_id, source.quote, source.image_id]).sort()).toEqual(
    brokenBefore.map((source) => [source.fragment_id, source.quote, source.image_id]).sort(),
  );
  const image = must(brokenAfter.find((source) => source.image_id !== null), 'источник-картинка');
  expect((await member.api.get(`/api/kb/documents/${broken.id}/images/${image.image_id}`)).status()).toBe(200);

  // Отправить на переобработку снова может только владелец: чужому личный документ не виден.
  await expectError(await colleague.api.post(`/api/kb/documents/${broken.id}/retry`), 404, 'not_found');
  const retried = await member.api.post(`/api/kb/documents/${broken.id}/retry`);
  expect(retried.status(), await retried.text()).toBe(200);
  expect(await retried.json()).toMatchObject({ status: 'ready', reprocess_error: null, reprocessing: true });
});
