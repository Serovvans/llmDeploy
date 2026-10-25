/**
 * «Память диалога» 0.7 — критерии 29–30 (docs/portal-design.md §10, §5.7; docs/portal-api.md §5.10).
 *
 * Что модель получила на самом деле, видно со стороны заглушки: её ответ называет число картинок в
 * запросе, а строка журнала `prompt usage` несёт `prompt_tokens` — символы всех текстовых частей
 * запроса, который получила заглушка, на 3 (docs/portal-api.md §12.2). Пары «Не учитывать» в
 * переписывании поискового запроса проверяются по найденному: заглушка переписывает уточнение по
 * последнему вопросу истории (§12.1).
 *
 * [ВМ]: пары «оценка — факт» на настоящей модели и уточнение коэффициентов — ручной чек-лист.
 */
import type { APIRequestContext, Page } from '@playwright/test';

import { newApi, uniqueSuffix } from '../support/accounts';
import { askCode, simpleProject } from '../support/code';
import { pngImage, textFile, textPdf } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import {
  addKbDocument,
  askChat,
  createDialog,
  eventsOf,
  expectError,
  parseStream,
  sourcesOf,
  stubReply,
  type StreamEvent,
} from '../support/portal';
import { promptUsage } from '../support/stand';
import { chooseKnowledge, expectAnswerReady } from '../support/ui';

type MemoryMode = 'full' | 'text_only' | 'excluded';

interface Pair {
  question_id: string;
  answer_id: string;
  attachments: number;
  memory_mode: MemoryMode;
  tokens: number;
  dropped: boolean;
}

interface Memory {
  level: 'free' | 'filling' | 'almost_full' | 'overflow';
  percent: number;
  used_tokens: number;
  limit_tokens: number;
  pairs: Pair[];
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status: string;
  dropped_messages: number;
  memory_mode: MemoryMode;
}

const FAST = 'mode=fast&search=false';
const FAST_SEARCH = 'mode=fast&search=true';
const THOROUGH = 'mode=thorough&search=false';

async function memoryOf(api: APIRequestContext, dialogId: string, query = FAST): Promise<Memory> {
  const response = await api.get(`/api/dialogs/${dialogId}/memory?${query}`);
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

/** Пара номер `n` (с 1) из оценки. */
function pairAt(memory: Memory, n: number): Pair {
  return must(memory.pairs[n - 1], `пара ${n}`);
}

async function setMode(api: APIRequestContext, dialogId: string, questionId: string, mode: MemoryMode): Promise<void> {
  const response = await api.patch(`/api/dialogs/${dialogId}/messages/${questionId}`, { data: { memory_mode: mode } });
  expect(response.status(), await response.text()).toBe(200);
  expect((await response.json()).memory_mode).toBe(mode);
}

/** Сообщения диалога от новых к старым. */
async function messagesOf(api: APIRequestContext, dialogId: string): Promise<Message[]> {
  return (await (await api.get(`/api/dialogs/${dialogId}/messages`)).json()).items;
}

async function lastAnswer(api: APIRequestContext, dialogId: string): Promise<Message> {
  const answer = must((await messagesOf(api, dialogId))[0], 'последнее сообщение');
  expect(answer.role).toBe('assistant');
  expect(answer.status).toBe('complete');
  return answer;
}

async function regenerate(api: APIRequestContext, dialogId: string): Promise<StreamEvent[]> {
  const response = await api.post(`/api/dialogs/${dialogId}/regenerate`);
  expect(response.status(), await response.text()).toBe(200);
  const events = parseStream(await response.text());
  expect(events.at(-1)?.event).toBe('done');
  return events;
}

/** `prompt_tokens` по заглушке для ответа: строка журнала пишется после конца потока. */
async function stubTokens(answerId: string, since: string): Promise<number> {
  await expect.poll(() => promptUsage(answerId, since)).not.toBeNull();
  return must(promptUsage(answerId, since), 'строка prompt usage').prompt_tokens;
}

/** Число картинок в запросе к модели — из ответа заглушки (docs/portal-api.md §12.2). */
function imagesSent(answer: Message): number {
  return Number(must(/Изображений: (\d+)\./.exec(answer.content), `число картинок в «${answer.content}»`)[1]);
}

/**
 * Чат из трёх длинных пар: «Быстро» без базы знаний — «заполняется», «Быстро» с поиском — «почти
 * заполнена», «Вдумчиво» — «переполнена». Длина третьего вопроса подбирается по пределам самого
 * стенда, чтобы тест не зависел от чисел конфигурации.
 */
async function threeLevelChat(api: APIRequestContext): Promise<string> {
  const dialogId = await createDialog(api, 'chat');
  const fast = (await memoryOf(api, dialogId, FAST)).limit_tokens;
  const search = (await memoryOf(api, dialogId, FAST_SEARCH)).limit_tokens;
  const thorough = (await memoryOf(api, dialogId, THOROUGH)).limit_tokens;
  // Нужен объём истории T: thorough < T < 0,8 × fast и 0,8 × search ≤ T ≤ search.
  const low = Math.max(thorough, 0.8 * search);
  const high = Math.min(0.8 * fast, search);
  expect(low, `пределы стенда ${JSON.stringify({ fast, search, thorough })} не дают трёх уровней`).toBeLessThan(high - 400);
  const target = (low + high) / 2;

  const chars = 30_000;
  for (const letter of ['Ж', 'З']) {
    await askChat(api, dialogId, letter.repeat(chars));
  }
  const two = await memoryOf(api, dialogId);
  const perPair = pairAt(two, 1).tokens;
  // Пара — вопрос и ответ; ответ заглушки не зависит от длины вопроса больше 200 символов.
  const overhead = perPair - chars / 2;
  const thirdChars = Math.round((target - two.used_tokens - overhead) * (chars / (perPair - overhead)));
  expect(thirdChars).toBeLessThanOrEqual(32_000);
  await askChat(api, dialogId, 'И'.repeat(thirdChars));

  expect((await memoryOf(api, dialogId, FAST)).level).toBe('filling');
  expect((await memoryOf(api, dialogId, FAST_SEARCH)).level).toBe('almost_full');
  expect((await memoryOf(api, dialogId, THOROUGH)).level).toBe('overflow');
  return dialogId;
}

/** Разбор документа через API; возвращает идентификатор его диалога, когда краткое содержание готово. */
async function docparseDialog(api: APIRequestContext, mark: string): Promise<string> {
  const response = await api.post('/api/docparse', {
    multipart: { file: textPdf(`lease-${mark}.pdf`, [`Lease ${mark}: forty nine years.`]), template_id: 'lease' },
  });
  expect(response.status(), 'разбор документа').toBe(200);
  const events = parseStream(await response.text());
  expect(events.at(-1)?.event).toBe('done');
  const dialogId = must(eventsOf(events, 'extraction')[0], 'событие extraction').data.dialog_id as string;
  const asked = await api.post(`/api/dialogs/${dialogId}/messages`, { data: { content: `Какой срок ${mark}?` } });
  expect(asked.status(), await asked.text()).toBe(200);
  expect(parseStream(await asked.text()).at(-1)?.event).toBe('done');
  return dialogId;
}

// ---------------------------------------------------------------------------------------------
// Критерий 29: индикатор и панель
// ---------------------------------------------------------------------------------------------

function meter(page: Page) {
  return page.getByRole('button', { name: /^Память диалога:/ });
}

function panel(page: Page) {
  return page.locator('[data-memory-panel]');
}

function panelToggle(page: Page) {
  return page.locator('[data-memory-toggle] button');
}

test.describe('критерий 29: индикатор и панель памяти', () => {
  test('индикатор во всех четырёх видах диалога; панель скрывается и открывается, положение переживает перезагрузку; на других экранах панели нет', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const mark = uniqueSuffix();
    const chatId = await createDialog(member.api, 'chat');
    await askChat(member.api, chatId, `Вопрос чата ${mark}`);
    const sqlId = await createDialog(member.api, 'sql');
    await member.api.post(`/api/dialogs/${sqlId}/messages`, {
      data: { content: `Участки ${mark}`, action: 'write', dialect: 'postgres', schema_id: null, test_run: false },
    });
    // С 0.9 четвёртый вид — помощник проекта раздела «Код» вместо помощника CoGIS.
    const codeProject = await simpleProject(member.api, 'MemoryPanel');
    await askCode(member.api, codeProject.dialog_id, `Слои ${mark}`);
    const docparseId = await docparseDialog(member.api, mark);

    // Первый вход: панель открыта.
    const page = await pageAs(member, `/chat/${chatId}`);
    await expect(meter(page)).toContainText('свободно');
    await expect(panel(page)).toBeVisible();
    await expect(panel(page)).toContainText('Свободно');
    await expect(panelToggle(page)).toHaveAttribute('aria-expanded', 'true');

    // Скрыта кнопкой в заголовке — и после перезагрузки скрыта; индикатор у поля ввода остаётся.
    await panelToggle(page).click();
    await expect(panel(page)).toHaveCount(0);
    await page.reload();
    await expect(meter(page)).toContainText('свободно');
    await expect(panel(page)).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem('portal.memoryPanel'))).toBe('hidden');

    // Положение одно на все экраны диалога: в SQL тоже скрыта; открывается нажатием на индикатор.
    await page.goto(`/sql/${sqlId}`);
    await expect(meter(page)).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
    await meter(page).click();
    await expect(panel(page)).toBeVisible();
    await page.reload();
    await expect(panel(page)).toBeVisible();

    // В разделе «Код» панель встаёт на место ленты помощника: при открытии проекта скрыта, положение
    // не запоминается (docs/portal-ui.md §5.13.12).
    await page.goto(`/code/${codeProject.id}`);
    await expect(meter(page)).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
    await meter(page).click();
    await expect(panel(page)).toBeVisible();
    await expect(panel(page).locator('[data-memory-select]')).toHaveCount(1);
    await page.reload();
    await expect(meter(page)).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
    expect(await page.evaluate(() => localStorage.getItem('portal.memoryPanel'))).not.toBe('hidden');

    await page.goto(`/documents/${docparseId}`);
    await expect(meter(page)).toBeVisible();
    await expect(panel(page)).toBeVisible();
    await expect(panel(page).locator('[data-memory-select]')).toHaveCount(1);

    // Закрытие крестиком в панели разбора — и снова после перезагрузки скрыта.
    await panel(page).getByRole('button', { name: 'Скрыть панель памяти' }).click();
    await expect(panel(page)).toHaveCount(0);
    await page.reload();
    await expect(meter(page)).toBeVisible();
    await expect(panel(page)).toHaveCount(0);
    await panelToggle(page).click();
    await expect(panel(page)).toBeVisible();

    for (const path of ['/knowledge', '/profile']) {
      await page.goto(path);
      await expect(page.getByRole('heading').first()).toBeVisible();
      await expect(panelToggle(page), path).toHaveCount(0);
      await expect(panel(page), path).toHaveCount(0);
      await expect(meter(page), path).toHaveCount(0);
    }
  });

  test('при 1280 px панель открывается поверх ленты, при 1920 px лента сужается на её ширину', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    await askChat(member.api, dialogId, 'Вопрос для ширины');
    const page = await pageAs(member, `/chat/${dialogId}`);
    // Колонка ленты с панелью запроса — соседка панели памяти (MemoryLayout); у поля ввода своя наибольшая ширина.
    const feedWidth = () =>
      page.evaluate(() => {
        const column = document.querySelector('[data-composer]')?.closest('[class*="_dialog_"]');
        return column ? column.getBoundingClientRect().width : null;
      });
    for (const [width, narrower] of [
      [1280, false],
      [1920, true],
    ] as const) {
      await page.setViewportSize({ width, height: 900 });
      await expect(panel(page)).toBeVisible();
      await expect(panel(page)).toHaveJSProperty('offsetWidth', 320);
      const open = await feedWidth();
      await panelToggle(page).click();
      await expect(panel(page)).toHaveCount(0);
      const hidden = await feedWidth();
      if (narrower) {
        expect(must(hidden, 'лента') - must(open, 'лента'), `${width} px`).toBe(320);
      } else {
        expect(open, `${width} px`).not.toBeNull();
        expect(open, `${width} px`).toBe(hidden);
      }
      await panelToggle(page).click();
    }
  });

  test('уровень меняется при смене режима ответа и выбора базы знаний; с «почти заполнена» под лентой заметка', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const dialogId = await threeLevelChat(member.api);
    const page = await pageAs(member, `/chat/${dialogId}`);

    await expect(meter(page)).toContainText('заполняется');
    await expect(page.getByText('Память диалога почти заполнена', { exact: false })).toHaveCount(0);

    await page.getByRole('switch', { name: 'Вдумчиво' }).click();
    await expect(meter(page)).toContainText('переполнена');
    await expect(page.getByText(/^Память диалога переполнена: со следующим вопросом/)).toBeVisible();
    // Все три пары помечены «Не поместится» не обязательно — хотя бы первая.
    await expect(panel(page).getByText('Не поместится').first()).toBeVisible();

    await page.getByRole('switch', { name: 'Быстро' }).click();
    await expect(meter(page)).toContainText('заполняется');

    await chooseKnowledge(page, ['Моя']);
    await expect(meter(page)).toContainText('почти заполнена');
    await expect(page.getByText(/^Память диалога почти заполнена: скоро/)).toBeVisible();
  });

  test('«переполнена» — следующий запрос отбрасывает ранние сообщения; «почти заполнена» — нет', async ({ newMember }) => {
    const member = await newMember();
    const dialogId = await threeLevelChat(member.api);
    const before = await memoryOf(member.api, dialogId, FAST_SEARCH);
    expect(before.pairs.every((pair) => !pair.dropped)).toBe(true);

    // «Почти заполнена» (поиск по личной базе): начало не отбрасывается.
    let since = new Date().toISOString();
    const nearFull = await askChat(member.api, dialogId, 'Дальше?', { collections: ['personal'] });
    expect(eventsOf(nearFull, 'context_truncated')).toEqual([]);
    const kept = await lastAnswer(member.api, dialogId);
    expect(kept.dropped_messages).toBe(0);
    const keptTokens = await stubTokens(kept.id, since);

    // «Переполнена» («Вдумчиво»): отбрасывается, ровно те пары, что помечены в оценке.
    const overflow = await memoryOf(member.api, dialogId, THOROUGH);
    expect(overflow.level).toBe('overflow');
    const markedDropped = overflow.pairs.filter((pair) => pair.dropped).length;
    expect(markedDropped).toBeGreaterThan(0);
    since = new Date().toISOString();
    await askChat(member.api, dialogId, 'Дальше?', { mode: 'thorough' });
    const cut = await lastAnswer(member.api, dialogId);
    expect(cut.dropped_messages).toBe(2 * markedDropped);
    // Заглушка получила меньше: первая пара (≈ 30 000 символов, 10 000 по заглушке) в запрос не вошла.
    expect(await stubTokens(cut.id, since)).toBeLessThan(keptTokens - 9_000);
  });
});

// ---------------------------------------------------------------------------------------------
// Критерий 30: учёт пар
// ---------------------------------------------------------------------------------------------

test.describe('критерий 30: учёт пар', () => {
  test('«Не учитывать» — пары нет в запросе; повторная генерация следует выбору в обе стороны', async ({ newMember }) => {
    const member = await newMember();
    const api = member.api;
    const dialogId = await createDialog(api, 'chat');
    await askChat(api, dialogId, 'Б'.repeat(20_000));
    await askChat(api, dialogId, 'Второй вопрос');
    const first = pairAt(await memoryOf(api, dialogId), 1);
    expect(first.tokens).toBeGreaterThan(9_000);

    await setMode(api, dialogId, first.question_id, 'excluded');
    const memory = await memoryOf(api, dialogId);
    expect(memory.pairs[0]).toMatchObject({ memory_mode: 'excluded', tokens: 0 });
    // Обе строки пары в ленте помечены как исключённые.
    const rows = (await messagesOf(api, dialogId)).filter((m) => m.id === first.question_id || m.id === first.answer_id);
    expect(rows.map((m) => m.memory_mode)).toEqual(['excluded', 'excluded']);

    let since = new Date().toISOString();
    await askChat(api, dialogId, 'Третий вопрос');
    const excluded = await lastAnswer(api, dialogId);
    // Исключённая пара — не «отброшенное по длине».
    expect(excluded.dropped_messages).toBe(0);
    const excludedTokens = await stubTokens(excluded.id, since);

    since = new Date().toISOString();
    await regenerate(api, dialogId);
    const again = await lastAnswer(api, dialogId);
    expect(again.id).not.toBe(excluded.id);
    expect(await stubTokens(again.id, since)).toBe(excludedTokens);

    await setMode(api, dialogId, first.question_id, 'full');
    since = new Date().toISOString();
    await regenerate(api, dialogId);
    const included = await lastAnswer(api, dialogId);
    // 20 000 символов вопроса и 200 в ответе — около 6 700 по заглушке.
    expect(await stubTokens(included.id, since)).toBeGreaterThan(excludedTokens + 6_600);
  });

  test('«Не учитывать» — пары нет и в переписывании поискового запроса', async ({ newMember }) => {
    const member = await newMember();
    const api = member.api;
    const mark = `zq${uniqueSuffix()}`;
    const question = `Что сказано в договоре ${mark}?`;
    const target = await addKbDocument(api, textFile(`договор-${mark}.txt`, `Что сказано в договоре ${mark}: срок сорок девять лет.`), {
      scope: 'personal',
    });
    // Восемь заметок со словами уточнения: без переписывания они занимают все 8 мест выдачи.
    const decoys = [];
    for (const letters of ['аа', 'бб', 'вв', 'гг', 'дд', 'ее', 'жж', 'зз']) {
      decoys.push(await addKbDocument(api, textFile(`заметка-${letters}-${mark}.txt`, `Уточни подробности: заметка ${letters}${mark}.`), { scope: 'personal' }));
    }
    const followUp = `Уточни подробности ${stubReply('Найдено: [1] [2] [3] [4] [5] [6] [7] [8]')}`;

    async function found(exclude: boolean): Promise<string[]> {
      const dialogId = await createDialog(api, 'chat');
      await askChat(api, dialogId, 'Здравствуйте');
      await askChat(api, dialogId, question);
      if (exclude) {
        const pairs = (await memoryOf(api, dialogId)).pairs;
        await setMode(api, dialogId, must(pairs[1], 'пара 2').question_id, 'excluded');
      }
      return sourcesOf(await askChat(api, dialogId, followUp, { collections: ['personal'] })).map((s) => s.document_id);
    }

    // Пара учитывается: уточнение переписано по ней и находит договор.
    expect(await found(false)).toContain(target.id);
    // Пара исключена: переписано по предыдущей («Здравствуйте») — договора в выдаче нет.
    const withoutPair = await found(true);
    expect(withoutPair).not.toContain(target.id);
    expect(withoutPair.length).toBeGreaterThan(0);
  });

  test('«Без вложений» — ни картинок, ни текста вложений; повторяемый вопрос уходит с вложениями', async ({ newMember }) => {
    const member = await newMember();
    const api = member.api;
    const mark = uniqueSuffix();
    const dialogId = await createDialog(api, 'chat');
    const upload = async (file: ReturnType<typeof textFile>) => {
      const response = await api.post(`/api/dialogs/${dialogId}/attachments`, { multipart: { file } });
      expect(response.status(), await response.text()).toBe(201);
      return (await response.json()).id as string;
    };
    const image = await upload(pngImage(`фото-${mark}.png`, mark));
    const note = await upload(textFile(`записка-${mark}.txt`, 'Ю'.repeat(9_000)));
    await askChat(api, dialogId, 'Что на фото и в записке?', { attachmentIds: [image, note] });
    await askChat(api, dialogId, 'Без вложений вопрос');
    const withFiles = pairAt(await memoryOf(api, dialogId), 1);
    const plain = pairAt(await memoryOf(api, dialogId), 2);
    expect(withFiles.attachments).toBe(2);

    // «Без вложений» у вопроса без вложений — отказ по полю.
    const refused = await api.patch(`/api/dialogs/${dialogId}/messages/${plain.question_id}`, { data: { memory_mode: 'text_only' } });
    expect(refused.status()).toBe(422);
    expect((await refused.json()).error.fields).toEqual([expect.objectContaining({ field: 'memory_mode', code: 'invalid_format' })]);

    let since = new Date().toISOString();
    await askChat(api, dialogId, 'Третий');
    const full = await lastAnswer(api, dialogId);
    expect(imagesSent(full)).toBe(1);
    const fullTokens = await stubTokens(full.id, since);

    await setMode(api, dialogId, withFiles.question_id, 'text_only');
    expect(pairAt(await memoryOf(api, dialogId), 1).tokens).toBeLessThan(withFiles.tokens);
    since = new Date().toISOString();
    await regenerate(api, dialogId);
    const textOnly = await lastAnswer(api, dialogId);
    expect(imagesSent(textOnly)).toBe(0);
    // Записка (9 000 символов — 3 000 по заглушке) не ушла, а вопрос пары ушёл: ответ на него — в истории.
    expect(await stubTokens(textOnly.id, since)).toBeLessThan(fullTokens - 2_900);

    // Повторная генерация ответа на сам вопрос «Без вложений»: он уходит целиком (docs/portal-api.md §5.10.1).
    const single = await createDialog(api, 'chat');
    const own = await (await api.post(`/api/dialogs/${single}/attachments`, { multipart: { file: pngImage(`ещё-${mark}.png`, `${mark}b`) } })).json();
    await askChat(api, single, 'Что на фото?', { attachmentIds: [own.id] });
    const only = pairAt(await memoryOf(api, single), 1);
    await setMode(api, single, only.question_id, 'text_only');
    await regenerate(api, single);
    expect(imagesSent(await lastAnswer(api, single))).toBe(1);
  });

  test('во время ответа выбор не меняется: API — 409, в панели выбор недоступен', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const api = member.api;
    const dialogId = await createDialog(api, 'chat');
    await askChat(api, dialogId, 'Первый вопрос');
    const first = pairAt(await memoryOf(api, dialogId), 1);

    const page = await pageAs(member, `/chat/${dialogId}`);
    const select = page.locator(`[data-memory-select="${first.question_id}"] button`);
    await expect(select).toBeEnabled();

    const stream = api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: 'Долгий ответ [[stub:slow]]', mode: 'fast', collections: [] },
    });
    await expect
      .poll(async () => (await messagesOf(api, dialogId)).some((m) => m.status === 'streaming'))
      .toBe(true);

    const busy = await api.patch(`/api/dialogs/${dialogId}/messages/${first.question_id}`, { data: { memory_mode: 'excluded' } });
    await expectError(busy, 409, 'generation_in_progress');

    await page.reload();
    await expect(select).toBeDisabled();
    await expect(panel(page)).toContainText('Пока формируется ответ, память не меняется');

    expect(parseStream(await (await stream).text()).at(-1)?.event).toBe('done');
    expect(pairAt(await memoryOf(api, dialogId), 1).memory_mode).toBe('full');
    await expect(select).toBeEnabled({ timeout: 15_000 });
  });

  test('выбор в панели приглушает пару в ленте и меняет оценку; «Учитывать» возвращает', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const api = member.api;
    const dialogId = await createDialog(api, 'chat');
    await askChat(api, dialogId, 'Г'.repeat(20_000));
    await askChat(api, dialogId, 'Второй вопрос');
    const first = pairAt(await memoryOf(api, dialogId), 1);

    const page = await pageAs(member, `/chat/${dialogId}`);
    await expectAnswerReady(page);
    const select = page.locator(`[data-memory-select="${first.question_id}"] button`);
    await expect(select).toHaveAccessibleName('Вопрос 1: как учитывать');
    await select.click();
    await expect(page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Без вложений' })).toHaveCount(0);
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Не учитывать' }).click();

    await expect(page.getByText('Модель не учитывает', { exact: true })).toHaveCount(1);
    await expect.poll(async () => pairAt(await memoryOf(api, dialogId), 1).memory_mode).toBe('excluded');
    await page.reload();
    await expect(page.getByText('Модель не учитывает', { exact: true })).toHaveCount(1);

    await select.click();
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Учитывать' }).first().click();
    await expect(page.getByText('Модель не учитывает', { exact: true })).toHaveCount(0);
    await expect.poll(async () => pairAt(await memoryOf(api, dialogId), 1).memory_mode).toBe('full');
  });
});

// ---------------------------------------------------------------------------------------------
// Изоляция новых маршрутов: чужой диалог — «не найдено»
// ---------------------------------------------------------------------------------------------

test.describe('изоляция маршрутов памяти', () => {
  async function ownerDialogs(owner: Member) {
    const mark = uniqueSuffix();
    const chatId = await createDialog(owner.api, 'chat');
    await askChat(owner.api, chatId, `Секретный вопрос ${mark}`);
    const pair = pairAt(await memoryOf(owner.api, chatId), 1);
    const sqlId = await createDialog(owner.api, 'sql');
    const docparseId = await docparseDialog(owner.api, mark);
    return { chatId, pair, sqlId, docparseId };
  }

  test('чужой диалог: оценка и смена режима — 404 и сотруднику, и администратору; режим владельца не меняется', async ({
    newMember,
  }) => {
    const owner = await newMember();
    const data = await ownerDialogs(owner);
    for (const stranger of [await newMember(), await newMember('admin')]) {
      for (const [path, query] of [
        [data.chatId, FAST],
        [data.sqlId, 'action=write&dialect=postgres'],
        [data.docparseId, ''],
      ]) {
        const read = await stranger.api.get(`/api/dialogs/${path}/memory?${query}`);
        expect(read.status(), `оценка ${path}`).toBe(404);
        expect(await read.text()).not.toContain('Секретный');
      }
      const patch = await stranger.api.patch(`/api/dialogs/${data.chatId}/messages/${data.pair.question_id}`, {
        data: { memory_mode: 'excluded' },
      });
      expect(patch.status()).toBe(404);
      // Чужое сообщение через свой диалог — тоже 404: сообщение ищется по владельцу диалога.
      const ownDialog = await createDialog(stranger.api, 'chat');
      const smuggled = await stranger.api.patch(`/api/dialogs/${ownDialog}/messages/${data.pair.question_id}`, {
        data: { memory_mode: 'excluded' },
      });
      expect(smuggled.status()).toBe(404);
    }
    expect(pairAt(await memoryOf(owner.api, data.chatId), 1).memory_mode).toBe('full');
  });

  test('свой диалог: ответ вместо вопроса и несуществующее — 404; без сессии — 401', async ({ newMember }) => {
    const owner = await newMember();
    const { chatId, pair } = await ownerDialogs(owner);
    const missing = '00000000-0000-4000-8000-000000000000';
    const body = { data: { memory_mode: 'excluded' } };
    expect((await owner.api.patch(`/api/dialogs/${chatId}/messages/${pair.answer_id}`, body)).status()).toBe(404);
    expect((await owner.api.patch(`/api/dialogs/${chatId}/messages/${missing}`, body)).status()).toBe(404);
    expect((await owner.api.get(`/api/dialogs/${missing}/memory?${FAST}`)).status()).toBe(404);

    const anonymous = await newApi();
    expect((await anonymous.get(`/api/dialogs/${chatId}/memory?${FAST}`)).status()).toBe(401);
    expect((await anonymous.patch(`/api/dialogs/${chatId}/messages/${pair.question_id}`, body)).status()).toBe(401);
    await anonymous.dispose();
  });
});
