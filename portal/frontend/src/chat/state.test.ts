import { describe, expect, it } from 'vitest';

import type { StreamEvent } from '../api/stream';
import type { Dialog, Message } from '../api/types';
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
    dropped_messages: 0,
    created_at: '2026-10-05T09:00:00Z',
    ...patch,
  };
}

const QUESTION: ChatAction = {
  type: 'generationStarted',
  id: ID,
  question: { content: 'Вопрос', attachments: [] },
  now: 1000,
};

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
    expect(state.history.items[0]?.title).toBe('Аренда участка');
  });

  it('поиск в базе знаний: фаза, число найденного сразу, под ответом — только упомянутые вне кода', () => {
    const source = (n: number) => ({
      n,
      document_id: `doc-${n}`,
      document_title: `Документ ${n}`,
      scope: 'shared' as const,
      page: n,
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
    const found = [{ n: 1, document_id: 'd', document_title: 'Д', scope: 'shared' as const, page: null, fragment_id: 'f', quote: 'ц' }];
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
