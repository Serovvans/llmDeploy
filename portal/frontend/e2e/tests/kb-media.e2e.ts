/**
 * Критерии 21, 23 и 24 (docs/portal-design.md §10): картинки из документов, записи в базе знаний
 * и во вложении чата. На стенде картинку описывает заглушка модели («Описание
 * изображения-заглушки <хеш>.»), речь «распознаёт» подмена («Расшифровка-заглушка, отрезок N.» на
 * каждые 30 секунд записи); ffmpeg — настоящий.
 *
 * Документы теста лежат в личной базе нового сотрудника: чужих картинок и записей в выдаче нет.
 */
import { uniqueSuffix } from '../support/accounts';
import { docxWithImage, pdfWithImage, pngImage, textFile, wavAudio, type TestFile } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import {
  addKbDocument,
  addKbRecording,
  answerText,
  askChat,
  attachRecording,
  chatWithAnswer,
  createDialog,
  declareUpload,
  expectError,
  kbMediaUrl,
  sourcesOf,
  stubReply,
  waitTranscription,
  type Source,
} from '../support/portal';
import { standMediaFiles, standVideo } from '../support/stand';
import { attachFiles, chooseKnowledge, composer, expectAnswerReady, fetchPlain, send } from '../support/ui';

const CITE_ALL = stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]');
const IMAGE_QUESTION = `Описание изображения-заглушки ${CITE_ALL}`;
const TWO_GIGABYTES = 2_147_483_648;

function token(): string {
  return `zq${uniqueSuffix()}`;
}

async function personalSources(member: Member, question: string): Promise<Source[]> {
  const { events } = await chatWithAnswer(member.api, question, { collections: ['personal'] });
  return sourcesOf(events);
}

/** Запись с допустимой сигнатурой WAV, но телом плейлиста: приём её пропускает, проверка воркера — нет. */
function playlistInWav(name: string, seed: string): TestFile {
  const body = Buffer.from(`#EXTM3U\n#EXTINF:10,${seed}\nhttp://bifrost:8080/health\n`, 'utf8');
  const header = Buffer.alloc(12);
  header.write('RIFF', 0, 'latin1');
  header.writeUInt32LE(4 + body.length, 4);
  header.write('WAVE', 8, 'latin1');
  return { name, mimeType: 'audio/wav', buffer: Buffer.concat([header, body]) };
}

async function documentState(member: Member, documentId: string) {
  return (await member.api.get(`/api/kb/documents/${documentId}`)).json();
}

async function waitDocumentError(member: Member, documentId: string): Promise<string> {
  await expect.poll(async () => (await documentState(member, documentId)).status, { timeout: 45_000 }).toBe('error');
  return (await documentState(member, documentId)).error_code;
}

test.describe('Картинки из документов', () => {
  test('картинка из PDF и из DOCX находится по описанию; чужому и администратору не отдаётся; картинка ниже порога не сохраняется', async ({
    newMember,
  }) => {
    const owner = await newMember();
    const colleague = await newMember();
    const admin = await newMember('admin');
    const seed = uniqueSuffix();
    const mark = `zq${seed}`;
    const pdf = await addKbDocument(owner.api, pdfWithImage(`plan-${mark}.pdf`, `Site plan ${mark} of the land parcel.`, seed), {
      scope: 'personal',
    });
    const docx = await addKbDocument(
      owner.api,
      docxWithImage(`справка-${mark}.docx`, `Справка ${mark} с печатью ведомства.`, seed.slice(4)),
      { scope: 'personal' },
    );
    const small = await addKbDocument(owner.api, pngImage(`значок-${mark}.png`, seed.slice(2)), { scope: 'personal' });

    const sources = await personalSources(owner, IMAGE_QUESTION);
    const images = sources.filter((source) => source.image_id !== null);
    expect(images.map((source) => source.document_id).sort()).toEqual([pdf.id, docx.id].sort());
    for (const image of images) {
      expect(image.quote).toMatch(/^Описание изображения-заглушки [0-9a-f]{8}\.$/);
    }
    expect(must(images.find((source) => source.document_id === pdf.id), 'картинка PDF').page).toBe(1);
    // Значок 32×32 ниже порога: документ найден текстом скана, картинки у него нет.
    expect(sources.some((source) => source.document_id === small.id && source.image_id === null)).toBe(true);

    for (const image of images) {
      const address = `/api/kb/documents/${image.document_id}/images/${image.image_id}`;
      const served = await owner.api.get(address);
      expect(served.status()).toBe(200);
      expect(served.headers()['content-type']).toBe('image/png');
      expect(served.headers()['x-content-type-options']).toBe('nosniff');
      expect((await served.body()).subarray(1, 4).toString('latin1')).toBe('PNG');
      for (const stranger of [colleague, admin]) {
        await expectError(await stranger.api.get(address), 404, 'not_found');
      }
      // Картинка обязана принадлежать документу из адреса.
      await expectError(await owner.api.get(`/api/kb/documents/${small.id}/images/${image.image_id}`), 404, 'not_found');
    }
  });

  test('ответ со сноской на картинку показывает её под ответом и в карточке источника; просмотр — картинку с описанием', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const seed = uniqueSuffix();
    const file = pdfWithImage(`scheme-zq${seed}.pdf`, `Boundary scheme zq${seed} of the parcel.`, seed);
    await addKbDocument(member.api, file, { scope: 'personal' });

    const page = await pageAs(member, '/chat');
    await chooseKnowledge(page, ['Моя']);
    await send(page, IMAGE_QUESTION);
    await expectAnswerReady(page);

    const loaded = (image: HTMLImageElement) => image.complete && image.naturalWidth > 0;
    const figure = page.getByRole('button', { name: new RegExp(`^Картинка из документа ${file.name}, источник \\d$`) });
    await expect(figure).toBeVisible();
    await expect.poll(() => figure.getByRole('img').evaluate(loaded)).toBe(true);
    await expect(figure.getByRole('img')).toHaveAttribute('src', /^\/api\/kb\/documents\/[0-9a-f-]+\/images\/[0-9a-f-]+$/);

    const card = page.getByRole('button', { name: new RegExp(`^\\d\\s*${file.name}`) }).filter({ has: page.getByRole('img') });
    await expect(card).toHaveCount(1);
    await expect(card).toContainText('Мой документ');
    await expect.poll(() => card.getByRole('img').evaluate(loaded)).toBe(true);

    await figure.click();
    const viewer = page.getByRole('dialog');
    await expect(viewer).toContainText(file.name);
    await expect(viewer.getByRole('img', { name: `Картинка из документа ${file.name}` })).toBeVisible();
    await expect(viewer).toContainText('Описание картинки');
    await expect(viewer).toContainText(/Описание изображения-заглушки [0-9a-f]{8}\./);
  });

  test('markdown-картинка из текста модели не загружается', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, '/chat');
    const foreign: string[] = [];
    page.on('request', (request) => {
      if (request.url().includes('example.invalid')) {
        foreign.push(request.url());
      }
    });
    await send(page, `Покажи схему ${stubReply('Схема: ![план участка](https://example.invalid/plan.png) — конец.')}`);
    await expectAnswerReady(page);
    // Разметка картинки выведена текстом: подпись и адрес, без элемента изображения.
    await expect(page.locator('main p').filter({ hasText: /^Схема: план участка/ })).toContainText('— конец.');
    await expect(page.locator('main img')).toHaveCount(0);
    expect(foreign).toEqual([]);
  });
});

test.describe('Записи в базе знаний', () => {
  test('аудио и видео после загрузки находятся поиском; источник несёт таймкод; исходник на диске не остаётся', async ({
    newMember,
  }) => {
    const owner = await newMember();
    const colleague = await newMember();
    const admin = await newMember('admin');
    const mark = token();
    const audioFile = wavAudio(`совещание-${mark}.wav`, 320, mark);
    const videoFile: TestFile = { name: `планёрка-${mark}.mkv`, mimeType: 'video/x-matroska', buffer: standVideo(35, mark) };

    const audio = await addKbRecording(owner.api, audioFile, { scope: 'personal', tags: [`записи-${mark}`] });
    const video = await addKbRecording(owner.api, videoFile, { scope: 'personal' });
    expect(await documentState(owner, audio.id)).toMatchObject({
      kind: 'recording',
      status: 'ready',
      duration_seconds: 320,
      page_count: null,
      tags: [`записи-${mark}`],
    });
    const videoState = await documentState(owner, video.id);
    expect(videoState).toMatchObject({ kind: 'recording', status: 'ready' });
    // Кодек дополняет звуковую дорожку до целого кадра: длительность — 35 секунд с округлением вверх.
    expect([35, 36]).toContain(videoState.duration_seconds);

    // Вторая часть расшифровки начинается на пятой минуте: её фрагмент несёт этот таймкод.
    const sources = await personalSources(owner, `Расшифровка-заглушка, отрезок 11 ${CITE_ALL}`);
    expect(sources.every((source) => source.timecode_seconds !== null && source.page === null)).toBe(true);
    expect(sources.map((source) => source.document_id)).toContain(video.id);
    const late = must(
      sources.find((source) => source.document_id === audio.id && source.timecode_seconds === 300),
      'фрагмент второй части записи',
    );
    expect(late.quote).toContain('Расшифровка-заглушка, отрезок 11.');

    const text = await (await owner.api.get(`/api/kb/documents/${audio.id}/text?timecode=300`)).json();
    expect(text).toMatchObject({ page: 2, page_count: 2, timecode_seconds: 300, recognized: true });
    expect(text.segments[0]).toMatchObject({ text: expect.stringContaining('отрезок 11.'), start_seconds: 300 });

    // Звук для прослушивания — Opus в Ogg, с перемоткой по Range; исходный файл не отдаётся и не хранится.
    for (const [recording, original] of [
      [audio, audioFile],
      [video, videoFile],
    ] as const) {
      const sound = await owner.api.get(`/api/kb/documents/${recording.id}/audio`);
      expect(sound.status()).toBe(200);
      expect(sound.headers()).toMatchObject({ 'content-type': 'audio/ogg', 'accept-ranges': 'bytes' });
      const size = (await sound.body()).length;
      expect((await sound.body()).subarray(0, 4).toString('latin1')).toBe('OggS');
      expect(size).toBeLessThan(original.buffer.length);
      const part = await owner.api.get(`/api/kb/documents/${recording.id}/audio`, { headers: { Range: 'bytes=100-199' } });
      expect(part.status()).toBe(206);
      expect(part.headers()['content-range']).toBe(`bytes 100-199/${size}`);
      expect((await part.body()).length).toBe(100);
      await expectError(await owner.api.get(`/api/kb/documents/${recording.id}/file`), 404, 'not_found');
      for (const stranger of [colleague, admin]) {
        await expectError(await stranger.api.get(`/api/kb/documents/${recording.id}/audio`), 404, 'not_found');
        await expectError(await stranger.api.get(`/api/kb/documents/${recording.id}/text`), 404, 'not_found');
      }
    }
    // В томе записей сотрудника — только сжатый звук двух записей: ни WAV, ни видео там нет.
    const stored = standMediaFiles(owner.account.id);
    expect(stored).toHaveLength(2);
    expect(Math.max(...stored)).toBeLessThan(Math.min(audioFile.buffer.length, videoFile.buffer.length));
  });

  test('карточка источника показывает момент записи; просмотр открывает расшифровку и проигрыватель на этом моменте', async ({
    newMember,
    pageAs,
    browserName,
  }) => {
    const member = await newMember();
    const mark = token();
    const file = wavAudio(`совещание-${mark}.wav`, 320, mark);

    // Запись добавляется в окне базы знаний: по умолчанию — в личную базу.
    const page = await pageAs(member, '/knowledge');
    await page.getByRole('button', { name: 'Добавить документы' }).first().click();
    const modal = page.getByRole('dialog');
    await modal.locator('input[type=file]').setInputFiles(file);
    await expect(modal.getByRole('radio', { name: /^В мою базу/ })).toBeChecked();
    await expect(modal).toContainText('Среди файлов есть запись.');
    await modal.getByRole('button', { name: 'Добавить', exact: true }).click();
    await page.getByText('Моя', { exact: true }).click();
    const row = page.getByRole('row').filter({ hasText: file.name });
    await expect(row).toContainText('Готов', { timeout: 45_000 });
    await expect(row).toContainText('5 мин');

    await page.goto('/chat');
    await chooseKnowledge(page, ['Моя']);
    await send(page, `Расшифровка-заглушка, отрезок 11 ${CITE_ALL}`);
    await expectAnswerReady(page);
    const card = page.getByRole('button', { name: new RegExp(`^\\d\\s*${file.name}`) }).filter({ hasText: '5:00' });
    await expect(card).toContainText('отрезок 11.');

    await card.click();
    const viewer = page.getByRole('dialog');
    await expect(viewer).toContainText('Часть 2 из 2');
    await expect(viewer).toContainText('Исходный файл не хранится — только звук и расшифровка');
    await expect(viewer.locator('mark').first()).toContainText('отрезок 11.');
    const player = viewer.locator('audio');
    await expect(player).toHaveAttribute('aria-label', `Запись ${file.name}`);
    await expect(player).toHaveAttribute('src', /\/api\/kb\/documents\/[0-9a-f-]+\/audio$/);

    // Opus в Ogg движок Safari не проигрывает; требование портала — Chrome, Edge и Firefox.
    test.skip(browserName === 'webkit', 'Opus в Ogg не поддерживается движком WebKit');
    const position = () => player.evaluate((element: HTMLAudioElement) => Math.round(element.currentTime));
    await expect.poll(() => player.evaluate((element: HTMLAudioElement) => element.readyState)).toBeGreaterThan(0);
    await expect.poll(position).toBe(300);
    expect(await player.evaluate((element: HTMLAudioElement) => element.paused)).toBe(true);
    // Момент в расшифровке первой части переставляет проигрыватель.
    await viewer.getByRole('button', { name: 'Предыдущая часть' }).click();
    await expect(viewer).toContainText('Часть 1 из 2');
    await viewer.getByRole('button', { name: /^Слушать с / }).filter({ hasText: '00:30' }).click();
    await expect.poll(position).toBe(30);
  });
});

test.describe('Записи в чате и отказы', () => {
  test('короткая запись во вложении попадает в вопрос текстом; пока расшифровка не готова, отправка недоступна', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const mark = token();
    const file = wavAudio(`звонок-${mark}.wav`, 40, mark);
    const page = await pageAs(member, '/chat');

    // Состояние вложения придерживается на пути к браузеру: расшифровка на стенде занимает доли секунды.
    let hold = true;
    await page.route(/\/api\/dialogs\/[^/]+\/attachments\/[^/]+$/, async (route) => {
      if (route.request().method() !== 'GET') {
        return route.continue();
      }
      const response = await fetchPlain(route);
      const attachment = await response.json();
      if (hold && attachment.transcription?.status === 'ready') {
        attachment.transcription.status = 'processing';
      }
      return route.fulfill({ response, json: attachment });
    });
    const sent: string[] = [];
    page.on('request', (request) => {
      if (request.method() === 'POST' && /\/messages$/.test(request.url())) {
        sent.push(request.url());
      }
    });

    await attachFiles(page, file);
    const chip = page.getByRole('status').filter({ hasText: /Ждёт расшифровки|Расшифровывается…/ });
    await expect(chip).toBeVisible();
    await composer(page).fill('О чём эта запись?');
    await page.getByRole('button', { name: 'Отправить', exact: true }).click({ force: true });
    await expect(
      page.getByText('Запись ещё расшифровывается — отправить сообщение можно будет, когда она будет готова'),
    ).toBeVisible();
    expect(sent).toEqual([]);

    hold = false;
    await expect(page.getByText('Расшифровано', { exact: true })).toBeVisible({ timeout: 30_000 });
    await send(page, 'О чём эта запись?');
    await expectAnswerReady(page);
    expect(sent).toHaveLength(1);
    await expect(page.locator('main')).toContainText('Расшифровка-заглушка, отрезок 1.');
    await expect(page.locator('main')).toContainText(file.name);
  });

  test('вложение-запись: расшифровка уходит модели; запись с ошибкой и чужая запись не отправляются', async ({ newMember }) => {
    const owner = await newMember();
    const stranger = await newMember('admin');
    const mark = token();
    const dialogId = await createDialog(owner.api, 'chat');

    const uploaded = await attachRecording(owner.api, dialogId, wavAudio(`звонок-${mark}.wav`, 65, mark));
    expect(uploaded.status(), await uploaded.text()).toBe(201);
    const attachment = await uploaded.json();
    expect(attachment).toMatchObject({ media_type: expect.stringMatching(/^audio\//), image_count: 0, page_count: null });
    expect(attachment.transcription.status).toMatch(/^(queued|processing|ready)$/);

    // Чужая запись, её состояние и загрузка в чужой диалог — 404, в том числе для администратора.
    const address = `/api/dialogs/${dialogId}/attachments/${attachment.id}`;
    await expectError(await stranger.api.get(address), 404, 'not_found');
    await expectError(await attachRecording(stranger.api, dialogId, wavAudio(`чужое-${mark}.wav`, 5, `x${mark}`)), 404, 'not_found');

    expect(await waitTranscription(owner.api, dialogId, attachment.id)).toMatchObject({
      transcription: { status: 'ready', error_code: null },
      duration_seconds: 65,
    });
    // Оригинал записи не хранится.
    await expectError(await owner.api.get(`${address}/file`), 404, 'not_found');
    const events = await askChat(owner.api, dialogId, 'Перескажи запись', { attachmentIds: [attachment.id] });
    expect(events.at(-1)?.event).toBe('done');
    expect(answerText(events)).toContain('Расшифровка-заглушка, отрезок 1. Расшифровка-заглушка, отрезок 2.');

    // Файл с сигнатурой WAV и телом плейлиста: приём проходит, проверка воркера отклоняет.
    const broken = await attachRecording(owner.api, dialogId, playlistInWav(`плейлист-${mark}.wav`, mark));
    expect(broken.status(), await broken.text()).toBe(201);
    const brokenId: string = (await broken.json()).id;
    expect((await waitTranscription(owner.api, dialogId, brokenId)).transcription).toEqual({
      status: 'error',
      error_code: 'media_invalid',
    });
    const refused = await owner.api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: 'А эта?', mode: 'fast', collections: [], attachment_ids: [brokenId] },
    });
    await expectError(refused, 409, 'attachment_not_ready');
    expect((await refused.json()).error.details.attachment_id).toBe(brokenId);
  });

  test('отказы при загрузке: недопустимый файл, плейлист, файл больше 2 ГБ — с объяснением', async ({ newMember }) => {
    const member = await newMember();
    const mark = token();
    const dialogId = await createDialog(member.api, 'chat');
    const chatUrl = `/api/dialogs/${dialogId}/attachments/media?file_name=${encodeURIComponent('запись.mp3')}`;
    const kbUrl = kbMediaUrl('запись.mp3', { scope: 'personal' });
    const notMedia: TestFile[] = [
      { ...textFile('запись.mp3', `Это не запись, а текст ${mark}.`), mimeType: 'audio/mpeg' },
      { ...textFile('эфир.m3u8', `#EXTM3U\n#EXTINF:10,${mark}\nhttp://bifrost:8080/health\n`), mimeType: 'audio/mpegurl' },
    ];

    for (const url of [chatUrl, kbUrl]) {
      for (const file of notMedia) {
        const refused = await member.api.post(url, { data: file.buffer, headers: { 'Content-Type': file.mimeType } });
        await expectError(refused, 415, 'unsupported_file_type');
        expect((await refused.json()).error.message).toMatch(/[а-яё]/i);
      }
      const tooLarge = await declareUpload(member.api, url, TWO_GIGABYTES + 1);
      expect(tooLarge.status, tooLarge.body).toBe(413);
      expect(JSON.parse(tooLarge.body).error).toMatchObject({ code: 'file_too_large', details: { max_bytes: TWO_GIGABYTES } });
    }
    // Ни один отказ ничего не создал и места не занял.
    expect((await (await member.api.get('/api/kb/documents?scope=personal')).json()).items).toEqual([]);
    expect(standMediaFiles(member.account.id)).toEqual([]);
  });

  test('запись длиннее предела отклоняется воркером: в чате — длиннее 20 минут, в базе знаний — длиннее 3 часов', async ({
    newMember,
  }) => {
    const member = await newMember();
    const mark = token();
    const dialogId = await createDialog(member.api, 'chat');

    // Частота 1 кГц: многочасовая запись остаётся файлом в несколько мегабайт.
    const longForChat = await attachRecording(member.api, dialogId, wavAudio(`лекция-${mark}.wav`, 21 * 60, mark, 1000));
    expect(longForChat.status(), await longForChat.text()).toBe(201);
    expect((await waitTranscription(member.api, dialogId, (await longForChat.json()).id)).transcription).toEqual({
      status: 'error',
      error_code: 'media_too_long',
    });

    const longForKb = wavAudio(`заседание-${mark}.wav`, 181 * 60, mark, 1000);
    const accepted = await member.api.post(kbMediaUrl(longForKb.name, { scope: 'personal' }), {
      data: longForKb.buffer,
      headers: { 'Content-Type': 'application/octet-stream' },
    });
    expect(accepted.status(), await accepted.text()).toBe(201);
    const documentId: string = (await accepted.json()).id;
    expect(await waitDocumentError(member, documentId)).toBe('media_too_long');
    // Исходник отклонённой записи удалён сразу; повторная обработка невозможна.
    await expect.poll(() => standMediaFiles(member.account.id)).toEqual([]);
    await expectError(await member.api.post(`/api/kb/documents/${documentId}/retry`), 409, 'source_unavailable');

    // Плейлист под сигнатурой WAV в базе знаний получает ошибку с причиной.
    const playlist = playlistInWav(`плейлист-${mark}.wav`, mark);
    const queued = await member.api.post(kbMediaUrl(playlist.name, { scope: 'personal' }), {
      data: playlist.buffer,
      headers: { 'Content-Type': 'application/octet-stream' },
    });
    expect(queued.status(), await queued.text()).toBe(201);
    expect(await waitDocumentError(member, (await queued.json()).id)).toBe('media_invalid');
  });
});
