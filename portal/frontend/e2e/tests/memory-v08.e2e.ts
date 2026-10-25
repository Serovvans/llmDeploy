/**
 * «Память диалога» 0.8 — критерии 31–32 (docs/portal-design.md §10, §5.7; docs/portal-api.md §5.10.5–5.10.8, §6.5).
 *
 * Что модель получила, видно со стороны заглушки: черновик пересказа — `Пересказ-заглушка <h>: сообщений <M>.`,
 * где M — число блоков вопросов и ответов в запросе пересказа; расход запроса ответа — строка журнала
 * `prompt usage` (`prompt_tokens` — символы всех текстовых частей на 3, docs/portal-api.md §12.2).
 *
 * Медленный черновик: пометка `[[stub:slow]]` действует на запрос пересказа, если она есть в пересказываемом
 * тексте. Тест кладёт её в перенесённое или в применённый пересказ — они уходят в черновик, но в ответах стоят
 * в головном сообщении, а не в последнем, и ответы не замедляют.
 *
 * [ВМ]: пересказ сохраняет числа, реквизиты и SQL; задержка соседних чатов во время сжатия — ручной чек-лист.
 * «Пересказ и перенесённое не попадают в системное сообщение» на стенде не наблюдаемо (заглушка не различает
 * роли); проверяется юнит-тестами бэкенда (portal/backend/tests/test_memory_v08.py).
 */
import type { APIRequestContext, Page } from '@playwright/test';

import { newApi, uniqueSuffix } from '../support/accounts';
import { simpleProject, wizardTool } from '../support/code';
import { textPdf } from '../support/files';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { askChat, askSql, createDialog, eventsOf, expectError, parseStream, type StreamEvent } from '../support/portal';
import { promptUsage } from '../support/stand';
import { expectAnswerReady, toast } from '../support/ui';

// С 0.9 помощник CoGIS — помощник проекта раздела «Код» (диалог `code`, docs/portal-api.md §7.5.8):
// новые диалоги `cogis` не создаются, перенос «в CoGIS» создаёт проект.
type Kind = 'chat' | 'sql' | 'code' | 'docparse';
type Target = Exclude<Kind, 'docparse'>;

interface Pair {
  question_id: string;
  answer_id: string;
  memory_mode: 'full' | 'text_only' | 'excluded';
  summarized: boolean;
  dropped: boolean;
}

interface Memory {
  level: 'free' | 'filling' | 'almost_full' | 'overflow';
  used_tokens: number;
  pairs: Pair[];
  compress_pairs: { from: number; to: number } | null;
}

interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  status: string;
  dropped_messages: number;
}

interface Summary {
  text: string;
  until_message_id: string;
  messages: number;
}

interface Carried {
  text: string;
  source_dialog_id: string | null;
  source_kind: Kind | null;
  source_title: string | null;
}

interface Draft {
  until: string | null;
  messages: number;
  dropped: number;
  text: string;
  truncated: boolean;
}

const MEMORY_QUERY: Record<Kind, string> = {
  chat: 'mode=fast&search=false',
  sql: 'action=write&dialect=postgres',
  code: 'action=write',
  docparse: '',
};
const STUB_SUMMARY = /^Пересказ-заглушка [0-9a-f]{8}: сообщений (\d+)\.$/;
const SLOW = '[[stub:slow]]';

async function memoryOf(api: APIRequestContext, dialogId: string, kind: Kind = 'chat'): Promise<Memory> {
  const response = await api.get(`/api/dialogs/${dialogId}/memory?${MEMORY_QUERY[kind]}`);
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

function pairAt(memory: Memory, n: number): Pair {
  return must(memory.pairs[n - 1], `пара ${n}`);
}

/** Сообщения диалога от новых к старым. */
async function messagesOf(api: APIRequestContext, dialogId: string): Promise<Message[]> {
  return (await (await api.get(`/api/dialogs/${dialogId}/messages?limit=100`)).json()).items;
}

async function lastAnswer(api: APIRequestContext, dialogId: string): Promise<Message> {
  const answer = must((await messagesOf(api, dialogId))[0], 'последнее сообщение');
  expect(answer.role).toBe('assistant');
  expect(answer.status).toBe('complete');
  return answer;
}

async function dialogOf(api: APIRequestContext, dialogId: string): Promise<{ title: string | null; summary: Summary | null; carried: Carried | null }> {
  const response = await api.get(`/api/dialogs/${dialogId}`);
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

/** Вопрос в диалоге любого вида, кроме разбора, через API; ждёт конца потока. */
async function ask(api: APIRequestContext, kind: Target, dialogId: string, content: string): Promise<StreamEvent[]> {
  if (kind === 'chat') {
    return askChat(api, dialogId, content);
  }
  if (kind === 'sql') {
    return askSql(api, dialogId, content);
  }
  const response = await api.post(`/api/dialogs/${dialogId}/messages`, { data: { content, action: 'write', file_ids: null, target: null } });
  expect(response.status(), await response.text()).toBe(200);
  return parseStream(await response.text());
}

/** Имена проектов по идентификатору диалога помощника: название источника `code` — имя проекта. */
const projectNames = new Map<string, string>();

/** Новый диалог вида `kind`; у `code` — диалог помощника нового проекта мастера. */
async function newDialog(api: APIRequestContext, kind: Target): Promise<string> {
  if (kind !== 'code') {
    return createDialog(api, kind);
  }
  const project = await simpleProject(api, `P${uniqueSuffix()}`);
  projectNames.set(project.dialog_id, project.name);
  return project.dialog_id;
}

/** `prompt_tokens` по заглушке для последнего ответа диалога, заданного после `since`. */
async function lastAnswerTokens(api: APIRequestContext, dialogId: string, since: string): Promise<number> {
  const answer = await lastAnswer(api, dialogId);
  await expect.poll(() => promptUsage(answer.id, since)).not.toBeNull();
  return must(promptUsage(answer.id, since), 'строка prompt usage').prompt_tokens;
}

async function docparseDialog(api: APIRequestContext, mark: string): Promise<string> {
  const response = await api.post('/api/docparse', {
    multipart: { file: textPdf(`lease-${mark}.pdf`, [`Lease ${mark}: forty nine years.`]), template_id: 'lease' },
  });
  expect(response.status(), 'разбор документа').toBe(200);
  const events = parseStream(await response.text());
  expect(events.at(-1)?.event).toBe('done');
  return must(eventsOf(events, 'extraction')[0], 'событие extraction').data.dialog_id as string;
}

async function askDocparse(api: APIRequestContext, dialogId: string, content: string): Promise<void> {
  const response = await api.post(`/api/dialogs/${dialogId}/messages`, { data: { content } });
  expect(response.status(), await response.text()).toBe(200);
  expect(parseStream(await response.text()).at(-1)?.event).toBe('done');
}

/** Проект «Кода» с одним вопросом и ответом помощника; возвращает идентификатор проекта. */
async function projectWithAnswer(api: APIRequestContext, mark: string): Promise<string> {
  const project = await simpleProject(api, `P${uniqueSuffix()}`);
  expect((await ask(api, 'code', project.dialog_id, `Вопрос code ${mark}`)).at(-1)?.event).toBe('done');
  return project.id;
}

/** Диалог указанного вида с одним вопросом и ответом. */
async function dialogWithAnswer(api: APIRequestContext, kind: Kind, mark: string): Promise<string> {
  if (kind === 'docparse') {
    const id = await docparseDialog(api, mark);
    await askDocparse(api, id, `Какой срок ${mark}?`);
    return id;
  }
  const id = await newDialog(api, kind);
  expect((await ask(api, kind, id, `Вопрос ${kind} ${mark}`)).at(-1)?.event).toBe('done');
  return id;
}

function draftRequest(api: APIRequestContext, dialogId: string, body: object, timeout?: number) {
  return api.post(`/api/dialogs/${dialogId}/memory/draft`, { data: body, ...(timeout ? { timeout } : {}) });
}

function readDraft(body: string): Draft {
  const events = parseStream(body);
  const start = must(events[0], 'событие start');
  expect(start.event).toBe('start');
  const end = must(events.at(-1), 'завершающее событие');
  expect(end.event, JSON.stringify(end.data)).toBe('done');
  return {
    until: start.data.until_message_id,
    messages: start.data.messages,
    dropped: start.data.dropped_messages,
    text: eventsOf(events, 'delta')
      .map((event) => event.data.text)
      .join(''),
    truncated: end.data.truncated,
  };
}

async function prepareDraft(api: APIRequestContext, dialogId: string, body: object): Promise<Draft> {
  const response = await draftRequest(api, dialogId, body);
  const text = await response.text();
  expect(response.status(), text).toBe(200);
  return readDraft(text);
}

/** Сколько вопросов и ответов заглушка получила в запросе пересказа. */
function blocksSent(draft: Draft): number {
  return Number(must(STUB_SUMMARY.exec(draft.text), `черновик «${draft.text}»`)[1]);
}

async function saveSummary(api: APIRequestContext, dialogId: string, text: string, until: string): Promise<Summary> {
  const response = await api.put(`/api/dialogs/${dialogId}/memory/summary`, { data: { text, until_message_id: until } });
  expect(response.status(), await response.text()).toBe(200);
  return response.json();
}

async function carry(api: APIRequestContext, kind: Target, text: string, source: string): Promise<string> {
  if (kind === 'code') {
    // Перенос в «Код» — новый проект из формы мастера (docs/portal-api.md §7.5.4).
    const wizard = { name: `P${uniqueSuffix()}`, tools: [wizardTool()] };
    const response = await api.post('/api/code/projects', { data: { source: 'wizard', wizard, carried: { text, source_dialog_id: source } } });
    expect(response.status(), await response.text()).toBe(201);
    return (await response.json()).project.dialog_id;
  }
  const response = await api.post('/api/dialogs', { data: { kind, carried: { text, source_dialog_id: source } } });
  expect(response.status(), await response.text()).toBe(201);
  return (await response.json()).id;
}

/** Чат с вопросами `questions`; возвращает его идентификатор. */
async function chatWith(api: APIRequestContext, questions: string[]): Promise<string> {
  const id = await createDialog(api, 'chat');
  for (const question of questions) {
    expect((await askChat(api, id, question)).at(-1)?.event).toBe('done');
  }
  return id;
}

/** Ждёт, пока у сотрудника снова можно начать черновик (отметка снимается по завершении потока). */
async function expectDraftFree(api: APIRequestContext): Promise<void> {
  const probe = await chatWith(api, ['Проба черновика']);
  await expect.poll(async () => (await draftRequest(api, probe, { purpose: 'transfer' })).status(), { timeout: 20_000 }).toBe(200);
}

function panel(page: Page) {
  return page.locator('[data-memory-panel]');
}

function panelToggle(page: Page) {
  return page.locator('[data-memory-toggle] button');
}

function meter(page: Page) {
  return page.getByRole('button', { name: /^Память диалога:/ });
}

// ---------------------------------------------------------------------------------------------
// Критерий 31: сжатие
// ---------------------------------------------------------------------------------------------

test.describe('критерий 31: сжатие', () => {
  test('черновик правится до применения; после применения пересказанные пары не уходят модели, лента цела; «Отменить сжатие» возвращает как было', async ({
    newMember,
  }) => {
    const { api } = await newMember();
    const id = await chatWith(api, ['А'.repeat(15_000), 'Б'.repeat(15_000), 'Третий вопрос', 'Четвёртый вопрос']);
    let since = new Date().toISOString();
    await askChat(api, id, 'Пятый вопрос');
    const fullTokens = await lastAnswerTokens(api, id, since);
    const feed = (await messagesOf(api, id)).map((m) => m.id);
    expect(feed).toHaveLength(10);

    // Оставляются две последние пары (memory.keep_recent_pairs): сжимаются 1–3.
    const before = await memoryOf(api, id);
    expect(before.compress_pairs).toEqual({ from: 1, to: 3 });
    const draft = await prepareDraft(api, id, { purpose: 'compress' });
    expect(draft).toMatchObject({ until: pairAt(before, 3).answer_id, messages: 6, dropped: 0, truncated: false });
    expect(blocksSent(draft)).toBe(6);
    // Черновик ничего не сохраняет.
    expect((await dialogOf(api, id)).summary).toBeNull();
    expect((await memoryOf(api, id)).pairs.some((pair) => pair.summarized)).toBe(false);

    // Проверки поля и границы — до записи.
    await expectError(await api.put(`/api/dialogs/${id}/memory/summary`, { data: { text: '   ', until_message_id: draft.until } }), 422, 'validation_error');
    const tooLong = await api.put(`/api/dialogs/${id}/memory/summary`, { data: { text: 'Ы'.repeat(8_001), until_message_id: draft.until } });
    expect(tooLong.status()).toBe(422);
    expect((await tooLong.json()).error.fields).toEqual([expect.objectContaining({ field: 'text', code: 'too_long' })]);
    // После последнего ответа нет двух вопросов — границей он быть не может.
    const lastBoundary = await api.put(`/api/dialogs/${id}/memory/summary`, {
      data: { text: 'Пересказ', until_message_id: pairAt(before, 5).answer_id },
    });
    expect(lastBoundary.status()).toBe(422);
    expect((await lastBoundary.json()).error.fields).toEqual([expect.objectContaining({ field: 'until_message_id', code: 'unknown_value' })]);
    expect((await dialogOf(api, id)).summary).toBeNull();

    // Правленый черновик применяется.
    const edited = `Правленый пересказ: участок 47:12:0101 и срок 49 лет. ${draft.text}`;
    expect(await saveSummary(api, id, edited, must(draft.until, 'граница'))).toEqual({ text: edited, until_message_id: draft.until, messages: 6 });
    expect((await dialogOf(api, id)).summary).toEqual({ text: edited, until_message_id: draft.until, messages: 6 });
    const applied = await memoryOf(api, id);
    expect(applied.pairs.map((pair) => pair.summarized)).toEqual([true, true, true, false, false]);
    expect(applied.compress_pairs).toBeNull();
    expect(applied.used_tokens).toBeLessThan(before.used_tokens - 9_000);

    // Лента цела.
    expect((await messagesOf(api, id)).map((m) => m.id)).toEqual(feed);

    // Следующий ответ: 30 000 символов пар 1–2 модели не уходят (≈ 10 000 по заглушке).
    since = new Date().toISOString();
    await askChat(api, id, 'Шестой вопрос');
    const compressed = await lastAnswer(api, id);
    expect(compressed.dropped_messages).toBe(0);
    const compressedTokens = await lastAnswerTokens(api, id, since);
    expect(compressedTokens).toBeLessThan(fullTokens - 9_000);

    // «Отменить сжатие»: пересказа нет, пары снова уходят модели.
    expect((await api.delete(`/api/dialogs/${id}/memory/summary`)).status()).toBe(204);
    expect((await api.delete(`/api/dialogs/${id}/memory/summary`)).status()).toBe(204);
    expect((await dialogOf(api, id)).summary).toBeNull();
    expect((await memoryOf(api, id)).pairs.some((pair) => pair.summarized)).toBe(false);
    since = new Date().toISOString();
    const regenerated = await api.post(`/api/dialogs/${id}/regenerate`);
    expect(parseStream(await regenerated.text()).at(-1)?.event).toBe('done');
    expect(await lastAnswerTokens(api, id, since)).toBeGreaterThan(compressedTokens + 9_000);
    expect((await messagesOf(api, id)).length).toBe(12);
  });

  test('пары «Не учитывать» в пересказ не входят; повторное сжатие пересказывает прежний пересказ с новыми парами', async ({ newMember }) => {
    const { api } = await newMember();
    const id = await chatWith(api, ['Первый', 'Второй', 'Третий', 'Четвёртый']);
    const memory = await memoryOf(api, id);
    const excluded = await api.patch(`/api/dialogs/${id}/messages/${pairAt(memory, 1).question_id}`, { data: { memory_mode: 'excluded' } });
    expect(excluded.status()).toBe(200);
    const draft = await prepareDraft(api, id, { purpose: 'compress' });
    // Граница — после пары 2; модели ушла только пара 2.
    expect(draft.until).toBe(pairAt(memory, 2).answer_id);
    expect(blocksSent(draft)).toBe(2);
    await saveSummary(api, id, `Старый пересказ ${uniqueSuffix()}`, must(draft.until, 'граница'));

    await askChat(api, id, 'Пятый');
    expect((await memoryOf(api, id)).compress_pairs).toEqual({ from: 3, to: 3 });
    const again = await prepareDraft(api, id, { purpose: 'compress' });
    expect(again.until).toBe(pairAt(memory, 3).answer_id);
    // Прежний пересказ — не блок вопроса или ответа; новая пара 3 — два блока.
    expect(blocksSent(again)).toBe(2);
    expect(again.text).not.toBe(draft.text);
  });

  test('незаконченный черновик ничего не меняет и отпускает сотрудника; сжимать нечего — 409', async ({ newMember }) => {
    const { api } = await newMember();
    const id = await chatWith(api, ['Один', 'Два', 'Три', 'Четыре']);
    const memory = await memoryOf(api, id);
    // Медленный пересказ: пометка — в применённом пересказе, повторное сжатие пересказывает его.
    const slowSummary = `Ранее ${SLOW} обсуждали участок.`;
    await saveSummary(api, id, slowSummary, pairAt(memory, 1).answer_id);
    const before = await memoryOf(api, id);
    expect(before.compress_pairs).toEqual({ from: 2, to: 2 });

    // Соединение закрывается до конца черновика — это отмена.
    await expect(draftRequest(api, id, { purpose: 'compress' }, 2_000)).rejects.toThrow();
    expect((await dialogOf(api, id)).summary).toMatchObject({ text: slowSummary, until_message_id: pairAt(memory, 1).answer_id });
    expect((await memoryOf(api, id)).pairs.map((pair) => pair.summarized)).toEqual(before.pairs.map((pair) => pair.summarized));
    await expectDraftFree(api);

    const fresh = await chatWith(api, ['Только один']);
    await expectError(await draftRequest(api, fresh, { purpose: 'compress' }), 409, 'nothing_to_summarize');
    expect((await memoryOf(api, fresh)).compress_pairs).toBeNull();
  });

  test('второй черновик того же сотрудника одновременно не запускается, у другого сотрудника — запускается', async ({ newMember }) => {
    const first = await newMember();
    const other = await newMember();
    const source = await chatWith(first.api, ['Исходный вопрос']);
    const slowDialog = await carry(first.api, 'chat', `Сведения ${SLOW} о долгом.`, source);
    await askChat(first.api, slowDialog, 'Вопрос после переноса');
    const second = await chatWith(first.api, ['Второй диалог']);

    const slow = draftRequest(first.api, slowDialog, { purpose: 'transfer' });
    // Пока медленный черновик идёт, второй — в другом диалоге того же сотрудника — отклоняется.
    await expect
      .poll(async () => (await draftRequest(first.api, second, { purpose: 'transfer' })).status(), { timeout: 15_000 })
      .toBe(429);
    await expectError(await draftRequest(first.api, second, { purpose: 'compress' }), 409, 'nothing_to_summarize');
    await expectError(await draftRequest(first.api, slowDialog, { purpose: 'transfer' }), 429, 'draft_in_progress');
    // Другой сотрудник не ждёт.
    const others = await chatWith(other.api, ['Чужой вопрос']);
    expect(blocksSent(await prepareDraft(other.api, others, { purpose: 'transfer' }))).toBe(2);

    const slowResponse = await slow;
    expect(slowResponse.status()).toBe(200);
    expect(readDraft(await slowResponse.text()).text).toMatch(/Пересказ-заглушка/);
    expect(blocksSent(await prepareDraft(first.api, second, { purpose: 'transfer' }))).toBe(2);
  });

  test('модель перегружена — событие model_overloaded, черновика нет; конец по длине — truncated', async ({ newMember }) => {
    const { api } = await newMember();
    const source = await chatWith(api, ['Источник']);
    const overloaded = await carry(api, 'chat', 'Сведения [[stub:overloaded]]', source);
    const response = await draftRequest(api, overloaded, { purpose: 'transfer' });
    expect(response.status()).toBe(200);
    const events = parseStream(await response.text());
    expect(events.map((event) => event.event)).toEqual(['start', 'error']);
    expect(events.at(-1)?.data.code).toBe('model_overloaded');
    expect((await dialogOf(api, overloaded)).summary).toBeNull();

    const long = await carry(api, 'chat', 'Сведения [[stub:length]]', source);
    expect((await prepareDraft(api, long, { purpose: 'transfer' })).truncated).toBe(true);
  });

  test('во время ответа: сжатие, применение и отмена — 409, черновик переноса разрешён', async ({ newMember }) => {
    const { api } = await newMember();
    const id = await chatWith(api, ['Раз', 'Два', 'Три']);
    const memory = await memoryOf(api, id);
    const answering = api.post(`/api/dialogs/${id}/messages`, { data: { content: `Долгий ответ ${SLOW}`, mode: 'fast', collections: [] } });
    await expect.poll(async () => (await messagesOf(api, id)).some((m) => m.status === 'streaming')).toBe(true);

    await expectError(await draftRequest(api, id, { purpose: 'compress' }), 409, 'generation_in_progress');
    await expectError(
      await api.put(`/api/dialogs/${id}/memory/summary`, { data: { text: 'Пересказ', until_message_id: pairAt(memory, 1).answer_id } }),
      409,
      'generation_in_progress',
    );
    await expectError(await api.delete(`/api/dialogs/${id}/memory/summary`), 409, 'generation_in_progress');
    // Перенос разрешён: идущий ответ ещё пуст и в пересказ не входит (docs/portal-api.md §5.10.5).
    const transfer = await prepareDraft(api, id, { purpose: 'transfer' });
    // Три готовые пары ушли в пересказ. Вопрос идущего ответа (с медленной пометкой) входит тоже: заглушка
    // повторяет текст по кругу — проверяется только начало.
    expect(Number(must(/^Пересказ-заглушка [0-9a-f]{8}: сообщений (\d+)\./.exec(transfer.text), 'черновик')[1])).toBeGreaterThanOrEqual(6);

    expect(parseStream(await (await answering).text()).at(-1)?.event).toBe('done');
    expect((await dialogOf(api, id)).summary).toBeNull();
  });

  test('в разборе документа вопрос со сжатием помещается', async ({ newMember }) => {
    const { api } = await newMember();
    const id = await docparseDialog(api, uniqueSuffix());
    // Длинные вопросы, пока следующий запрос не начнёт отбрасывать начало.
    let memory = await memoryOf(api, id, 'docparse');
    for (let n = 0; n < 12 && memory.level !== 'overflow'; n += 1) {
      await askDocparse(api, id, `${n} ${'Ч'.repeat(30_000)}`);
      memory = await memoryOf(api, id, 'docparse');
    }
    expect(memory.level).toBe('overflow');
    expect(memory.pairs.some((pair) => pair.dropped)).toBe(true);
    expect(memory.compress_pairs).not.toBeNull();

    const draft = await prepareDraft(api, id, { purpose: 'compress' });
    expect(draft.text).toMatch(STUB_SUMMARY);
    await saveSummary(api, id, draft.text, must(draft.until, 'граница'));
    const compressed = await memoryOf(api, id, 'docparse');
    expect(compressed.level).not.toBe('overflow');
    expect(compressed.pairs.some((pair) => pair.dropped)).toBe(false);

    await askDocparse(api, id, 'Какой срок аренды?');
    expect((await lastAnswer(api, id)).dropped_messages).toBe(0);
  });

  test('сжатие в интерфейсе: черновик потоком, правка, «Применить», граница в ленте, «Отменить сжатие»; закрытое окно ничего не меняет', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const { api } = member;
    const id = await chatWith(api, ['Первый вопрос', 'Второй вопрос', 'Третий вопрос', 'Четвёртый вопрос']);
    const page = await pageAs(member, `/chat/${id}`);
    await expectAnswerReady(page);

    await panel(page).getByRole('button', { name: 'Сжать раннюю часть' }).click();
    const modal = page.getByRole('dialog');
    await expect(modal).toContainText('Сжать раннюю часть');
    const field = modal.getByRole('textbox', { name: 'Пересказ ранней части' });
    await expect(field).toHaveValue(STUB_SUMMARY);
    const edited = `Правка сотрудника ${uniqueSuffix()}`;
    await field.fill(edited);
    await modal.getByRole('button', { name: 'Применить' }).click();
    await expect(toast(page, 'Ранняя часть сжата')).toBeVisible();
    await expect(modal).toHaveCount(0);
    expect((await dialogOf(api, id)).summary?.text).toBe(edited);

    await page.reload();
    await expect(page.getByText('Выше — 4 сообщения, модель помнит их кратко')).toBeVisible();
    await expect(panel(page)).toContainText('Пересказ ранней части');
    await expect(panel(page)).toContainText('Вопросы 1–2 — в пересказе');
    // Лента цела: все четыре вопроса на месте.
    for (const question of ['Первый вопрос', 'Второй вопрос', 'Третий вопрос', 'Четвёртый вопрос']) {
      await expect(page.getByText(question, { exact: true }).first()).toBeAttached();
    }

    await panel(page).getByRole('button', { name: 'Отменить сжатие' }).click();
    await expect(modal).toContainText('Отменить сжатие?');
    await modal.getByRole('button', { name: 'Отменить сжатие' }).click();
    await expect(toast(page, 'Сжатие отменено')).toBeVisible();
    await expect(page.getByText(/^Выше — \d+ сообщ/)).toHaveCount(0);
    expect((await dialogOf(api, id)).summary).toBeNull();

    // Закрытое до конца черновика окно: прежний пересказ (с медленной пометкой) не меняется.
    const memory = await memoryOf(api, id);
    const slowSummary = `Ранее ${SLOW} говорили о сроках.`;
    await saveSummary(api, id, slowSummary, pairAt(memory, 1).answer_id);
    await page.reload();
    await panel(page).getByRole('button', { name: 'Сжать раннюю часть' }).click();
    await expect(modal.getByText('Модель пересказывает…')).toBeVisible();
    await modal.getByRole('button', { name: 'Отмена' }).click();
    await expect(modal).toHaveCount(0);
    expect((await dialogOf(api, id)).summary).toMatchObject({ text: slowSummary, until_message_id: pairAt(memory, 1).answer_id });
    await expectDraftFree(api);
  });
});

// ---------------------------------------------------------------------------------------------
// Критерий 32: перенос
// ---------------------------------------------------------------------------------------------

test.describe('критерий 32: перенос', () => {
  test('из каждого вида диалога в новый чат, SQL и «Код» — пересказом и одним ответом; в разбор — нельзя', async ({ newMember }) => {
    const { api } = await newMember();
    const mark = uniqueSuffix();
    for (const kind of ['chat', 'sql', 'code', 'docparse'] as const) {
      const source = await dialogWithAnswer(api, kind, mark);
      const title = kind === 'code' ? must(projectNames.get(source), 'имя проекта') : (await dialogOf(api, source)).title;
      const draft = await prepareDraft(api, source, { purpose: 'transfer' });
      // Разбор: краткое содержание с реквизитами — не блоки вопроса или ответа; один вопрос с ответом.
      expect(blocksSent(draft), kind).toBe(2);
      const verbatim = (await lastAnswer(api, source)).content;
      for (const target of ['chat', 'sql', 'code'] as const) {
        for (const text of [draft.text, verbatim]) {
          const created = await carry(api, target, text, source);
          expect(await dialogOf(api, created), `${kind} → ${target}`).toMatchObject({
            carried: { text, source_dialog_id: source, source_kind: kind, source_title: title },
            summary: null,
          });
        }
      }
      await expectError(await api.post('/api/dialogs', { data: { kind: 'docparse', carried: { text: draft.text, source_dialog_id: source } } }), 422, 'validation_error');
    }
    // Диалоги с перенесённым — в истории, хотя сообщений в них нет: «брошенными» они не считаются.
    for (const target of ['chat', 'sql', 'code'] as const) {
      const address = target === 'code' ? '/api/code/projects?limit=100' : `/api/dialogs?kind=${target}&limit=100`;
      const list = (await (await api.get(address)).json()).items as unknown[];
      // 4 источника × 2 способа и ещё сам источник того же вида.
      expect(list.length, target).toBe(9);
    }
  });

  test('перенесённое учитывается в каждом ответе и не исключается — в чате, SQL и «Код»', async ({ newMember }) => {
    const { api } = await newMember();
    const source = await chatWith(api, ['Источник']);
    // 6 000 символов — 2 000 по заглушке.
    const carried = `Сведения: ${'Ф'.repeat(6_000)}`;
    for (const kind of ['chat', 'sql', 'code'] as const) {
      const plain = await newDialog(api, kind);
      const withCarried = await carry(api, kind, carried, source);
      for (const question of ['Первый вопрос', 'Второй вопрос']) {
        let since = new Date().toISOString();
        await ask(api, kind, plain, question);
        const plainTokens = await lastAnswerTokens(api, plain, since);
        since = new Date().toISOString();
        await ask(api, kind, withCarried, question);
        expect(await lastAnswerTokens(api, withCarried, since), `${kind}: ${question}`).toBeGreaterThan(plainTokens + 1_900);
      }
      // Перенесённое — не пара: в описи две пары, выбора у сведений нет; все пары «Не учитывать» — сведения всё равно уходят.
      const memory = await memoryOf(api, withCarried, kind);
      expect(memory.pairs).toHaveLength(2);
      for (const pair of memory.pairs) {
        expect((await api.patch(`/api/dialogs/${withCarried}/messages/${pair.question_id}`, { data: { memory_mode: 'excluded' } })).status()).toBe(200);
      }
      const since = new Date().toISOString();
      await ask(api, kind, withCarried, 'Третий вопрос');
      expect(await lastAnswerTokens(api, withCarried, since), kind).toBeGreaterThan(2_000);
      // Сжимать нечего: перенесённое не сжимается.
      await expectError(await draftRequest(api, withCarried, { purpose: 'compress' }), 409, 'nothing_to_summarize');
    }
  });

  test('«Продолжить отсюда» берёт диалог до выбранного ответа; граница — только ответ этого диалога', async ({ newMember }) => {
    const owner = await newMember();
    const { api } = owner;
    const id = await chatWith(api, ['Первый', 'Второй', 'Третий']);
    const memory = await memoryOf(api, id);
    for (const [n, blocks] of [
      [1, 2],
      [2, 4],
      [3, 6],
    ] as const) {
      const draft = await prepareDraft(api, id, { purpose: 'transfer', until_message_id: pairAt(memory, n).answer_id });
      expect(draft).toMatchObject({ until: pairAt(memory, n).answer_id, messages: blocks });
      expect(blocksSent(draft)).toBe(blocks);
    }
    const whole = await prepareDraft(api, id, { purpose: 'transfer' });
    expect(whole).toMatchObject({ until: pairAt(memory, 3).answer_id, messages: 6 });

    const other = await chatWith(api, ['Другой диалог']);
    const otherAnswer = pairAt(await memoryOf(api, other), 1).answer_id;
    const stranger = await newMember();
    const strangerChat = await chatWith(stranger.api, ['Чужой']);
    const foreignAnswer = pairAt(await memoryOf(stranger.api, strangerChat), 1).answer_id;
    for (const until of [pairAt(memory, 1).question_id, otherAnswer, foreignAnswer, '00000000-0000-4000-8000-000000000000']) {
      const response = await draftRequest(api, id, { purpose: 'transfer', until_message_id: until });
      expect(response.status(), until).toBe(422);
      expect((await response.json()).error.fields).toEqual([expect.objectContaining({ field: 'until_message_id', code: 'unknown_value' })]);
    }

    // Пересказ до ответа 1 становится перенесённым нового диалога.
    const upToFirst = await prepareDraft(api, id, { purpose: 'transfer', until_message_id: pairAt(memory, 1).answer_id });
    const created = await carry(api, 'chat', upToFirst.text, id);
    expect((await dialogOf(api, created)).carried?.text).toBe(upToFirst.text);
  });

  test('удаление источника не ломает новый диалог', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const { api } = member;
    const source = await chatWith(api, ['Источник для удаления']);
    const title = must((await dialogOf(api, source)).title, 'название источника');
    const text = `Сведения о сроке ${uniqueSuffix()}`;
    const created = await carry(api, 'sql', text, source);
    await ask(api, 'sql', created, 'До удаления');

    expect((await api.delete(`/api/dialogs/${source}`)).status()).toBe(204);
    expect((await dialogOf(api, created)).carried).toEqual({ text, source_dialog_id: null, source_kind: null, source_title: title });
    const since = new Date().toISOString();
    expect((await ask(api, 'sql', created, 'После удаления')).at(-1)?.event).toBe('done');
    expect(await lastAnswerTokens(api, created, since)).toBeGreaterThan(text.length / 3);

    const page = await pageAs(member, `/sql/${created}`);
    await expect(page.getByText(`${title}» — модель учитывает их в каждом ответе`, { exact: false })).toBeVisible();
    // Ссылки на удалённый источник нет.
    await expect(page.getByRole('link', { name: title, exact: true })).toHaveCount(0);
  });

  test('перенос в интерфейсе: пересказом из чата в SQL, одним ответом из «Код» в чат, из разбора в чат, «Продолжить отсюда» в SQL', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const { api } = member;
    const mark = uniqueSuffix();
    const chat = await dialogWithAnswer(api, 'chat', mark);
    const page = await pageAs(member, `/chat/${chat}`);
    await expectAnswerReady(page);
    const modal = page.getByRole('dialog');

    // Чат → SQL пересказом, через меню заголовка.
    await page.locator('header').getByRole('button', { name: /^Действия: / }).click();
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Перенести в новый диалог…' }).click();
    await expect(modal).toContainText('Перенести в новый диалог');
    await modal.getByText('SQL-помощник', { exact: true }).click();
    await modal.getByRole('button', { name: 'Подготовить пересказ' }).click();
    const summaryField = modal.getByRole('textbox', { name: 'Пересказ диалога' });
    await expect(summaryField).toHaveValue(/^Пересказ-заглушка [0-9a-f]{8}: сообщений 2\.$/);
    const edited = `Поправленный пересказ ${mark}`;
    await summaryField.fill(edited);
    await modal.getByRole('button', { name: 'Перенести' }).click();
    await expect(page).toHaveURL(/\/sql\/[0-9a-f-]{36}$/);
    const sqlId = must(/\/sql\/([0-9a-f-]{36})$/.exec(page.url()), 'адрес SQL')[1] as string;
    expect((await dialogOf(api, sqlId)).carried).toMatchObject({ text: edited, source_dialog_id: chat, source_kind: 'chat' });
    await expect(page.getByText('— модель учитывает их в каждом ответе', { exact: false })).toBeVisible();
    await expect(panel(page)).toContainText('Модель учитывает их в каждом ответе');

    // «Код» → чат одним ответом, через кнопку панели памяти.
    const codeProject = await simpleProject(api, `P${uniqueSuffix()}`);
    const codeDialog = codeProject.dialog_id;
    expect((await ask(api, 'code', codeDialog, `Вопрос code ${mark}`)).at(-1)?.event).toBe('done');
    const codeAnswer = (await lastAnswer(api, codeDialog)).content;
    await page.goto(`/code/${codeProject.id}`);
    // В «Коде» панель при открытии проекта скрыта (docs/portal-ui.md §5.13.12).
    await meter(page).click();
    await panel(page).getByRole('button', { name: 'Перенести в новый диалог…' }).click();
    await modal.getByText('Чат', { exact: true }).click();
    await modal.getByText('Один ответ дословно', { exact: true }).click();
    await expect(modal.getByRole('textbox', { name: 'Один ответ дословно' })).toHaveValue(codeAnswer);
    await modal.getByRole('button', { name: 'Перенести' }).click();
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    const fromCode = must(/\/chat\/([0-9a-f-]{36})$/.exec(page.url()), 'адрес чата')[1] as string;
    expect((await dialogOf(api, fromCode)).carried).toMatchObject({ text: codeAnswer, source_kind: 'code' });

    // Разбор → чат (вид по умолчанию) пересказом.
    const docparse = await dialogWithAnswer(api, 'docparse', mark);
    await page.goto(`/documents/${docparse}`);
    await panel(page).getByRole('button', { name: 'Перенести в новый диалог…' }).click();
    await expect(modal).toContainText('Краткое содержание и таблица реквизитов войдут в пересказ');
    await modal.getByRole('button', { name: 'Подготовить пересказ' }).click();
    await expect(modal.getByRole('textbox', { name: 'Пересказ диалога' })).toHaveValue(STUB_SUMMARY);
    await modal.getByRole('button', { name: 'Перенести' }).click();
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    const fromDocparse = must(/\/chat\/([0-9a-f-]{36})$/.exec(page.url()), 'адрес чата')[1] as string;
    expect((await dialogOf(api, fromDocparse)).carried).toMatchObject({ source_dialog_id: docparse, source_kind: 'docparse' });

    // SQL: «Продолжить отсюда» у первого из двух ответов — вид по умолчанию SQL.
    const sql = await dialogWithAnswer(api, 'sql', mark);
    await ask(api, 'sql', sql, `Второй вопрос ${mark}`);
    await page.goto(`/sql/${sql}`);
    await expect(page.getByRole('button', { name: 'Продолжить отсюда' })).toHaveCount(2);
    await page.getByRole('button', { name: 'Продолжить отсюда' }).first().click();
    await expect(modal).toContainText('Продолжить в новом диалоге');
    await modal.getByRole('button', { name: 'Подготовить пересказ' }).click();
    await expect(modal.getByRole('textbox', { name: 'Пересказ диалога' })).toHaveValue(/сообщений 2\.$/);
    await modal.getByRole('button', { name: 'Перенести' }).click();
    await expect(page).toHaveURL(new RegExp(`/sql/(?!${sql})[0-9a-f-]{36}$`));
  });
});

// ---------------------------------------------------------------------------------------------
// Панель памяти: положение переживает перезагрузку во всех видах диалога
// ---------------------------------------------------------------------------------------------

test('панель памяти: скрыть и показать — положение переживает перезагрузку в чате, SQL и разборе, в «Коде» не запоминается', async ({ newMember, pageAs }) => {
  const member = await newMember();
  const mark = uniqueSuffix();
  const paths = [
    `/chat/${await dialogWithAnswer(member.api, 'chat', mark)}`,
    `/sql/${await dialogWithAnswer(member.api, 'sql', mark)}`,
    `/code/${await projectWithAnswer(member.api, mark)}`,
    `/documents/${await dialogWithAnswer(member.api, 'docparse', mark)}`,
  ];
  const page = await pageAs(member, must(paths[0], 'путь'));
  for (const path of paths) {
    if (path.startsWith('/code/')) {
      // В «Коде» панель при открытии скрыта и положение не запоминается (docs/portal-ui.md §5.13.12).
      await page.goto(path);
      await expect(meter(page), path).toBeVisible();
      await expect(panel(page), path).toHaveCount(0);
      await meter(page).click();
      await expect(panel(page), path).toBeVisible();
      await page.reload();
      await expect(panel(page), path).toHaveCount(0);
      continue;
    }
    await page.goto(path);
    await expect(panel(page), path).toBeVisible();
    await panelToggle(page).click();
    await expect(panel(page), path).toHaveCount(0);
    await page.reload();
    await expect(meter(page), path).toBeVisible();
    await expect(panel(page), path).toHaveCount(0);
    await meter(page).click();
    await expect(panel(page), path).toBeVisible();
    await page.reload();
    await expect(panel(page), path).toBeVisible();
  }
});

// ---------------------------------------------------------------------------------------------
// Изоляция маршрутов 0.8: чужой диалог — «не найдено»
// ---------------------------------------------------------------------------------------------

test.describe('изоляция маршрутов памяти 0.8', () => {
  async function ownerData(owner: Member) {
    const id = await chatWith(owner.api, ['Секретный первый', 'Секретный второй', 'Секретный третий']);
    const memory = await memoryOf(owner.api, id);
    const summary = await saveSummary(owner.api, id, 'Секретный пересказ', pairAt(memory, 1).answer_id);
    const carried = await carry(owner.api, 'chat', 'Секретные сведения', id);
    return { id, memory, summary, carried };
  }

  test('чужой диалог: черновик, применение, отмена сжатия, источник переноса и чтение — 404 сотруднику и администратору', async ({ newMember }) => {
    const owner = await newMember();
    const data = await ownerData(owner);
    for (const stranger of [await newMember(), await newMember('admin')]) {
      const before = (await (await stranger.api.get('/api/dialogs?kind=chat')).json()).items.length;
      const responses = [
        await draftRequest(stranger.api, data.id, { purpose: 'compress' }),
        await draftRequest(stranger.api, data.id, { purpose: 'transfer' }),
        await draftRequest(stranger.api, data.id, { purpose: 'transfer', until_message_id: pairAt(data.memory, 1).answer_id }),
        await stranger.api.put(`/api/dialogs/${data.id}/memory/summary`, { data: { text: 'Подмена', until_message_id: pairAt(data.memory, 1).answer_id } }),
        await stranger.api.delete(`/api/dialogs/${data.id}/memory/summary`),
        await stranger.api.post('/api/dialogs', { data: { kind: 'chat', carried: { text: 'Кража', source_dialog_id: data.id } } }),
        await stranger.api.get(`/api/dialogs/${data.carried}`),
      ];
      for (const response of responses) {
        const body = await response.text();
        expect(response.status(), `${response.url()}: ${body}`).toBe(404);
        expect(body).not.toContain('Секрет');
      }
      // Своим диалогом чужая граница не подставляется.
      const own = await chatWith(stranger.api, ['Свой 1', 'Свой 2', 'Свой 3']);
      const smuggled = await stranger.api.put(`/api/dialogs/${own}/memory/summary`, {
        data: { text: 'Подмена', until_message_id: pairAt(data.memory, 1).answer_id },
      });
      expect(smuggled.status()).toBe(422);
      expect((await (await stranger.api.get('/api/dialogs?kind=chat')).json()).items.length).toBe(before + 1);
    }
    expect((await dialogOf(owner.api, data.id)).summary).toEqual(data.summary);
  });

  test('без сессии — 401; несуществующий диалог и источник — 404', async ({ newMember }) => {
    const owner = await newMember();
    const data = await ownerData(owner);
    const missing = '00000000-0000-4000-8000-000000000000';
    await expectError(await draftRequest(owner.api, missing, { purpose: 'transfer' }), 404, 'not_found');
    await expectError(await owner.api.put(`/api/dialogs/${missing}/memory/summary`, { data: { text: 'x', until_message_id: missing } }), 404, 'not_found');
    await expectError(await owner.api.delete(`/api/dialogs/${missing}/memory/summary`), 404, 'not_found');
    await expectError(await owner.api.post('/api/dialogs', { data: { kind: 'chat', carried: { text: 'x', source_dialog_id: missing } } }), 404, 'not_found');

    const anonymous = await newApi();
    for (const response of [
      await draftRequest(anonymous, data.id, { purpose: 'transfer' }),
      await anonymous.put(`/api/dialogs/${data.id}/memory/summary`, { data: { text: 'x', until_message_id: pairAt(data.memory, 1).answer_id } }),
      await anonymous.delete(`/api/dialogs/${data.id}/memory/summary`),
      await anonymous.post('/api/dialogs', { data: { kind: 'chat', carried: { text: 'x', source_dialog_id: data.id } } }),
    ]) {
      expect(response.status(), response.url()).toBe(401);
    }
    await anonymous.dispose();
    expect((await dialogOf(owner.api, data.id)).summary).toEqual(data.summary);
  });
});
