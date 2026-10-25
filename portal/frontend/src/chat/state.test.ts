import { describe, expect, it } from 'vitest';

import type { StreamEvent } from '../api/stream';
import type { Dialog, Message, SqlRunBlock } from '../api/types';
import { chatReducer, historyGroup, INITIAL_STATE, type ChatAction, type ChatState } from './state';

const ID = 'd-1';

function run(actions: ChatAction[], from: ChatState = INITIAL_STATE): ChatState {
  return actions.reduce(chatReducer, from);
}

function events(start: number, ...list: [StreamEvent, number][]): ChatAction[] {
  return list.map(([event, offset]) => ({ type: 'streamEvent', id: ID, event, now: start + offset }));
}

function message(patch: Partial<Message>): Message {
  return {
    id: 'm',
    role: 'assistant',
    content: '',
    status: 'complete',
    error_code: null,
    reasoning: null,
    reasoning_seconds: null,
    attachments: [],
    sources: null,
    sources_found: null,
    sql_check: null,
    sql_run: null,
    sql_dangers: null,
    dropped_messages: 0,
    memory_mode: 'full',
    created_at: '2026-10-05T09:00:00Z',
    ...patch,
  };
}

const QUESTION: ChatAction = {
  type: 'generationStarted',
  id: ID,
  question: { content: 'Вопрос', attachments: [], withTags: false, testRun: false },
  now: 1000,
};

function runBlock(patch: Partial<SqlRunBlock>): SqlRunBlock {
  return {
    index: 0,
    line: 1,
    status: 'passed',
    reason: null,
    fixture: null,
    result: null,
    affected_rows: null,
    error: null,
    fixed_sql: null,
    ...patch,
  };
}

describe('проверка SQL на тестовых данных', () => {
  const ANSWERED: ChatAction[] = [
    QUESTION,
    ...events(
      1000,
      [{ type: 'start', user_message_id: 'u-1', assistant_message_id: 'a-1' }, 10],
      [{ type: 'delta', text: '```sql\nSELECT 1\n```' }, 20],
      [{ type: 'sql_check', blocks: [] }, 30],
    ),
  ];
  const HALF_DONE: StreamEvent = {
    type: 'sql_run',
    blocks: [
      runBlock({ index: 0, line: 1, status: 'passed' }),
      runBlock({ index: 1, line: 5, status: 'running' }),
      runBlock({ index: 2, line: 9, status: 'pending' }),
    ],
  };

  it('первое событие sql_run: ответ дописан, диалог занят; каждое событие заменяет предыдущее; done называет состояние', () => {
    let state = run(ANSWERED);
    expect(state.dialogs[ID]?.messages.at(-1)?.status).toBe('streaming');

    state = run(events(1000, [{ type: 'sql_run', blocks: [runBlock({ status: 'pending' })] }, 40]), state);
    expect(state.dialogs[ID]?.generation?.phase).toBe('checking');
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'complete', sql_run: { blocks: [{ status: 'pending' }] } });

    state = run(events(1000, [HALF_DONE, 50], [{ type: 'done', status: 'length_limit' }, 60]), state);
    expect(state.dialogs[ID]?.generation).toBeNull();
    expect(state.dialogs[ID]?.messages.at(-1)?.status).toBe('length_limit');
    expect(state.dialogs[ID]?.messages.at(-1)?.sql_run?.blocks.map((block) => block.status)).toEqual(['passed', 'running', 'pending']);
  });

  it.each<[string, ChatAction]>([
    ['остановка', { type: 'generationStopped', id: ID }],
    ['потеря сессии', { type: 'streamEvent', id: ID, event: { type: 'error', code: 'session_ended', message: '' }, now: 2000 }],
  ])('%s во время проверки: ответ цел, блоки без итога — «не завершена», готовый итог остаётся', (_name, action) => {
    const state = run([...ANSWERED, ...events(1000, [HALF_DONE, 40]), action]);
    const answer = state.dialogs[ID]?.messages.at(-1);
    expect(state.dialogs[ID]?.generation).toBeNull();
    expect(answer).toMatchObject({ status: 'complete', error_code: null, content: '```sql\nSELECT 1\n```' });
    expect(answer?.sql_run?.blocks.map((block) => block.status)).toEqual(['passed', 'stopped', 'stopped']);
  });

  it.each<[string, ChatAction]>([
    ['остановка', { type: 'generationStopped', id: ID }],
    ['потеря сессии', { type: 'streamEvent', id: ID, event: { type: 'error', code: 'session_ended', message: '' }, now: 2000 }],
  ])('вопрос с проверкой: ответ дописан уже по sql_check — %s сразу после него ответ не трогает', (_name, action) => {
    const asked: ChatAction = { ...QUESTION, question: { content: 'Вопрос', attachments: [], withTags: false, testRun: true } };
    let state = run([asked, ...ANSWERED.slice(1)]);
    expect(state.dialogs[ID]?.generation?.phase).toBe('checking');
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'complete', sql_check: { blocks: [] }, sql_run: null });

    state = run([action], state);
    expect(state.dialogs[ID]?.generation).toBeNull();
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'complete', error_code: null, sql_run: null });
  });

  it('остановка до проверки — обычная остановка ответа', () => {
    const state = run([...ANSWERED, { type: 'generationStopped', id: ID }]);
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'stopped', sql_run: null });
  });
});

describe('помощник проекта: правка и цикл исправления', () => {
  const ANSWERED: ChatAction[] = [
    QUESTION,
    ...events(
      1000,
      [{ type: 'start', user_message_id: 'u-1', assistant_message_id: 'a-1' }, 10],
      [{ type: 'delta', text: 'Не хватало «;».' }, 20],
    ),
  ];
  const FIX: StreamEvent = { type: 'code_fix', stage: 'building', attempt: 0, max_attempts: 2 };
  const EDIT: StreamEvent = {
    type: 'code_edit',
    status: 'proposed',
    files: [{ path: 'A.cs', change: 'modified', decision: null }],
    rejected_files: [],
    incomplete: false,
    verification: null,
    decided_at: null,
  };

  it('code_fix: ответ дописан, диалог занят шагом цикла; code_edit кладёт правку в ответ; done завершает', () => {
    let state = run([...ANSWERED, ...events(1000, [FIX, 30])]);
    expect(state.dialogs[ID]?.generation).toMatchObject({ phase: 'fixing', fix: { stage: 'building', attempt: 0, maxAttempts: 2 } });
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'complete', content: 'Не хватало «;».' });

    state = run(
      events(1000, [{ type: 'code_fix', stage: 'fixing', attempt: 1, max_attempts: 2 }, 40], [EDIT, 50], [{ type: 'done', status: 'complete' }, 60]),
      state,
    );
    expect(state.dialogs[ID]?.generation).toBeNull();
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({
      status: 'complete',
      code_edit: { status: 'proposed', files: [{ path: 'A.cs' }] },
    });
  });

  it('правка без цикла («Написать или изменить»): code_edit перед done', () => {
    const state = run([...ANSWERED, ...events(1000, [EDIT, 30], [{ type: 'done', status: 'complete' }, 40])]);
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'complete', code_edit: { status: 'proposed' } });
  });

  it.each<[string, ChatAction]>([
    ['остановка', { type: 'generationStopped', id: ID }],
    ['ошибка', { type: 'streamEvent', id: ID, event: { type: 'error', code: 'model_error', message: '' }, now: 2000 }],
  ])('%s во время цикла: ответ остаётся готовым, без пометки об остановке и без правки', (_name, action) => {
    const state = run([...ANSWERED, ...events(1000, [FIX, 30]), action]);
    expect(state.dialogs[ID]?.generation).toBeNull();
    const answer = state.dialogs[ID]?.messages.at(-1);
    expect(answer).toMatchObject({ status: 'complete', error_code: null, content: 'Не хватало «;».' });
    expect(answer?.code_edit ?? null).toBeNull();
  });

  it('остановка до цикла — обычная остановка ответа', () => {
    const state = run([...ANSWERED, { type: 'generationStopped', id: ID }]);
    expect(state.dialogs[ID]?.messages.at(-1)?.status).toBe('stopped');
  });
});

describe('состояние чата', () => {
  it('порядок событий: start → размышления → текст → done', () => {
    const state = run([
      QUESTION,
      ...events(
        1000,
        [{ type: 'start', user_message_id: 'u-1', assistant_message_id: 'a-1' }, 10],
        [{ type: 'reasoning_delta', text: 'Нужно ' }, 1000],
        [{ type: 'reasoning_delta', text: 'найти срок' }, 2000],
        [{ type: 'delta', text: 'Срок — ' }, 4400],
        [{ type: 'delta', text: '49 лет' }, 5000],
        [{ type: 'done', status: 'complete' }, 6000],
      ),
    ]);
    const view = state.dialogs[ID];
    expect(view?.generation).toBeNull();
    expect(view?.messages).toMatchObject([
      { id: 'u-1', role: 'user', content: 'Вопрос', status: 'complete' },
      {
        id: 'a-1',
        role: 'assistant',
        content: 'Срок — 49 лет',
        reasoning: 'Нужно найти срок',
        reasoning_seconds: 3,
        status: 'complete',
      },
    ]);
  });

  it('фазы: отправка → размышления → ответ; размышления после текста фазу не возвращают', () => {
    const phase = (state: ChatState) => state.dialogs[ID]?.generation?.phase;
    let state = run([QUESTION]);
    expect(phase(state)).toBe('sending');
    state = run(events(1000, [{ type: 'reasoning_delta', text: 'а' }, 100]), state);
    expect(phase(state)).toBe('thinking');
    state = run(events(1000, [{ type: 'delta', text: 'б' }, 200]), state);
    expect(phase(state)).toBe('answering');
    state = run(events(1000, [{ type: 'reasoning_delta', text: 'в' }, 300], [{ type: 'delta', text: 'г' }, 400]), state);
    expect(phase(state)).toBe('answering');
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ content: 'бг', reasoning: 'ав' });
  });

  it('ответ без размышлений не получает их длительности', () => {
    const state = run([QUESTION, ...events(1000, [{ type: 'delta', text: 'Сразу' }, 50])]);
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ reasoning: null, reasoning_seconds: null });
  });

  it('context_truncated, length_limit и название из события title', () => {
    const dialog: Dialog = { id: ID, kind: 'chat', title: null, created_at: '', updated_at: '' };
    const state = run([
      { type: 'dialogUpserted', dialog, toTop: true },
      QUESTION,
      ...events(
        1000,
        [{ type: 'context_truncated', dropped_messages: 4 }, 10],
        [{ type: 'title', title: 'Аренда участка' }, 20],
        [{ type: 'delta', text: 'Текст' }, 30],
        [{ type: 'done', status: 'length_limit' }, 40],
      ),
    ]);
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ dropped_messages: 4, status: 'length_limit' });
    expect(state.history.chat.items[0]?.title).toBe('Аренда участка');
  });

  it('поиск в базе знаний: фаза, число найденного сразу, под ответом — только упомянутые вне кода', () => {
    const source = (n: number) => ({
      n,
      document_id: `doc-${n}`,
      document_title: `Документ ${n}`,
      scope: 'shared' as const,
      page: n,
      timecode_seconds: null,
      image_id: null,
      fragment_id: `f-${n}`,
      quote: `Цитата ${n}`,
    });
    let state = run([QUESTION, ...events(1000, [{ type: 'search_started' }, 10])]);
    expect(state.dialogs[ID]?.generation?.phase).toBe('searching');

    state = run(events(1000, [{ type: 'sources', sources: [source(1), source(2), source(3)] }, 20]), state);
    // К «Отправляю…» строка состояния не возвращается.
    expect(state.dialogs[ID]?.generation?.phase).toBe('preparing');
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ sources_found: 3, sources: null });

    state = run(
      events(
        1000,
        [{ type: 'delta', text: 'Срок — 49 лет [1]. В коде `a[2]` не сноска. Ещё [3] и [7].' }, 30],
        [{ type: 'done', status: 'complete' }, 40],
      ),
      state,
    );
    expect(state.dialogs[ID]?.messages.at(-1)?.sources?.map((item) => item.n)).toEqual([1, 3]);
  });

  it('остановленный ответ: источники — по сноскам в полученной части; без поиска источников нет', () => {
    const found = [{ n: 1, document_id: 'd', document_title: 'Д', scope: 'shared' as const, page: null, timecode_seconds: null, image_id: null, fragment_id: 'f', quote: 'ц' }];
    const stopped = run([
      QUESTION,
      ...events(1000, [{ type: 'sources', sources: found }, 10], [{ type: 'delta', text: 'Пока без ссылок' }, 20]),
      { type: 'generationStopped', id: ID },
    ]);
    expect(stopped.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'stopped', sources: [], sources_found: 1 });

    const plain = run([QUESTION, ...events(1000, [{ type: 'delta', text: 'Ответ [1]' }, 10], [{ type: 'done', status: 'complete' }, 20])]);
    expect(plain.dialogs[ID]?.messages.at(-1)).toMatchObject({ sources: null, sources_found: null });
  });

  it('событие error: часть текста остаётся, код и текст сервера сохраняются', () => {
    const state = run([
      QUESTION,
      ...events(
        1000,
        [{ type: 'delta', text: 'Начало' }, 10],
        [{ type: 'error', code: 'model_unavailable', message: 'Модель недоступна.' }, 20],
      ),
    ]);
    expect(state.dialogs[ID]).toMatchObject({ generation: null });
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({
      content: 'Начало',
      status: 'error',
      error_code: 'model_unavailable',
      error_message: 'Модель недоступна.',
    });
  });

  it('остановка помечает ответ остановленным; после завершения ничего не меняет', () => {
    const stopped = run([QUESTION, ...events(1000, [{ type: 'delta', text: 'Часть' }, 10]), { type: 'generationStopped', id: ID }]);
    expect(stopped.dialogs[ID]?.messages.at(-1)).toMatchObject({ content: 'Часть', status: 'stopped' });
    expect(stopped.dialogs[ID]?.generation).toBeNull();

    const done = run([QUESTION, ...events(1000, [{ type: 'done', status: 'complete' }, 10]), { type: 'generationStopped', id: ID }]);
    expect(done.dialogs[ID]?.messages.at(-1)?.status).toBe('complete');
  });

  it('события после завершения потока не применяются', () => {
    const state = run([
      QUESTION,
      ...events(1000, [{ type: 'done', status: 'complete' }, 10], [{ type: 'delta', text: 'лишнее' }, 20]),
    ]);
    expect(state.dialogs[ID]?.messages.at(-1)?.content).toBe('');
  });

  it('повторная генерация заменяет последний ответ; отказ возвращает прежний', () => {
    const loaded = run([
      {
        type: 'messagesLoaded',
        id: ID,
        older: false,
        nextCursor: null,
        items: [message({ id: 'a-1', content: 'Старый ответ' }), message({ id: 'u-1', role: 'user', content: 'Вопрос' })],
      },
    ]);
    const before = loaded.dialogs[ID]?.messages ?? [];
    const started = run([{ type: 'generationStarted', id: ID, question: null, now: 5 }], loaded);
    expect(started.dialogs[ID]?.messages).toMatchObject([{ id: 'u-1' }, { status: 'streaming', content: '' }]);

    const rejected = run([{ type: 'generationRejected', id: ID, messages: before }], started);
    expect(rejected.dialogs[ID]?.messages).toEqual(before);
    expect(rejected.dialogs[ID]?.generation).toBeNull();
  });

  it('были ли у вопроса теги: «Ответить заново» наследует признак, ответ из истории его не имеет', () => {
    const asked = run([{ ...QUESTION, question: { content: 'Вопрос', attachments: [], withTags: true, testRun: false } } as ChatAction]);
    expect(asked.dialogs[ID]?.messages.at(-1)?.asked_with_tags).toBe(true);
    const again = run(
      [...events(1000, [{ type: 'done', status: 'complete' }, 10]), { type: 'generationStarted', id: ID, question: null, now: 2000 }],
      asked,
    );
    expect(again.dialogs[ID]?.messages.at(-1)).toMatchObject({ status: 'streaming', asked_with_tags: true });

    const fromHistory = run([
      { type: 'messagesLoaded', id: ID, older: false, nextCursor: null, items: [message({ id: 'a-1' }), message({ id: 'u-1', role: 'user' })] },
      { type: 'generationStarted', id: ID, question: null, now: 5 },
    ]);
    expect(fromHistory.dialogs[ID]?.messages.at(-1)?.asked_with_tags).toBeUndefined();
  });

  it('обновление последней страницы не теряет ранее подгруженные сообщения', () => {
    const state = run([
      { type: 'messagesLoaded', id: ID, older: false, nextCursor: 'c1', items: [message({ id: 'm3', created_at: '2026-10-05T09:03:00Z', status: 'streaming' })] },
      { type: 'messagesLoaded', id: ID, older: true, nextCursor: null, items: [message({ id: 'm2', created_at: '2026-10-05T09:02:00Z' }), message({ id: 'm1', created_at: '2026-10-05T09:01:00Z' })] },
      { type: 'messagesLoaded', id: ID, older: false, nextCursor: 'c1', items: [message({ id: 'm3', created_at: '2026-10-05T09:03:00Z', content: 'Готово' })] },
    ]);
    expect(state.dialogs[ID]?.messages.map((item) => item.id)).toEqual(['m1', 'm2', 'm3']);
    expect(state.dialogs[ID]?.messages.at(-1)).toMatchObject({ content: 'Готово', status: 'complete' });
    expect(state.dialogs[ID]?.olderCursor).toBeNull();
  });

  it('черновик нового чата переезжает к созданному чату', () => {
    const state = run([
      { type: 'draftUpdated', key: 'new', update: (draft) => ({ ...draft, text: 'Набранный вопрос' }) },
      { type: 'draftMoved', from: 'new', to: ID },
    ]);
    expect(state.drafts).toEqual({ [ID]: { text: 'Набранный вопрос', attachments: [], notice: null } });
  });

  it('группы истории: сегодня, вчера, на этой неделе (с понедельника), раньше', () => {
    const thursday = new Date(2026, 9, 8, 15, 0);
    const at = (day: number, hour = 12) => new Date(2026, 9, day, hour).toISOString();
    expect(historyGroup(at(8, 0), thursday)).toBe('today');
    expect(historyGroup(at(7, 23), thursday)).toBe('yesterday');
    expect(historyGroup(at(5, 0), thursday)).toBe('week');
    expect(historyGroup(at(4, 23), thursday)).toBe('earlier');
    // В понедельник и вторник «на этой неделе» пусто: всё раньше вчера — «раньше».
    expect(historyGroup(at(4), new Date(2026, 9, 6, 10))).toBe('earlier');
  });
});

describe('память диалога в ленте', () => {
  const loaded: ChatAction = {
    type: 'messagesLoaded',
    id: ID,
    // Сервер отдаёт от новых к старым.
    items: [
      message({ id: 'a-2' }),
      message({ id: 'u-2', role: 'user' }),
      message({ id: 'a-1' }),
      message({ id: 'u-1', role: 'user' }),
    ],
    nextCursor: null,
    older: false,
  };

  it('режим пары меняется у вопроса и его ответа, соседняя пара не затронута', () => {
    const state = run([loaded, { type: 'memoryModeChanged', id: ID, questionId: 'u-1', mode: 'excluded' }]);
    expect(state.dialogs[ID]?.messages.map((item) => [item.id, item.memory_mode])).toEqual([
      ['u-1', 'excluded'],
      ['a-1', 'excluded'],
      ['u-2', 'full'],
      ['a-2', 'full'],
    ]);
  });

  it('пара старше загруженных сообщений ленту не меняет', () => {
    const before = run([loaded]);
    expect(chatReducer(before, { type: 'memoryModeChanged', id: ID, questionId: 'u-0', mode: 'excluded' })).toEqual(before);
  });

  it('новый ответ повторной генерации несёт режим своего вопроса, ответ на новый вопрос — «Учитывать»', () => {
    const excluded = run([loaded, { type: 'memoryModeChanged', id: ID, questionId: 'u-2', mode: 'text_only' }]);
    const regenerated = chatReducer(excluded, { type: 'generationStarted', id: ID, question: null, now: 1 });
    expect(regenerated.dialogs[ID]?.messages.at(-1)?.memory_mode).toBe('text_only');
    const asked = chatReducer(excluded, {
      type: 'generationStarted',
      id: ID,
      question: { content: 'Ещё вопрос', attachments: [], withTags: false, testRun: false },
      now: 1,
    });
    expect(asked.dialogs[ID]?.messages.slice(-2).map((item) => item.memory_mode)).toEqual(['full', 'full']);
  });
});
