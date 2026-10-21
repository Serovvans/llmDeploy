import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import type { DraftEvent } from '../api/stream';
import { texts } from '../texts';
import { draftExit, draftNotice, draftReducer, IDLE_DRAFT, type DraftState } from './draft';

const t = texts.memory;

function run(events: DraftEvent[], from: DraftState = draftReducer(IDLE_DRAFT, { type: 'begin' })): DraftState {
  return events.reduce((state, event) => draftReducer(state, { type: 'event', event }), from);
}

const COMPRESS = { purpose: 'compress' as const, keepRecent: 2, verbatim: false };
const TRANSFER = { purpose: 'transfer' as const, keepRecent: 2, verbatim: true };

describe('черновик пересказа: поток', () => {
  it('черновик — конкатенация delta; граница и урезанный вход — из start, обрезка — из done', () => {
    const state = run([
      { type: 'start', until_message_id: 'a-6', messages: 12, dropped_messages: 3 },
      { type: 'delta', text: 'Срок аренды ' },
      { type: 'delta', text: '— 49 лет.' },
      { type: 'done', truncated: true },
    ]);
    expect(state).toMatchObject({ phase: 'ready', text: 'Срок аренды — 49 лет.', until: 'a-6', dropped: 3, truncated: true });
  });

  it('остановка и обрыв оставляют полученный текст; новый черновик решает заново про урезанный вход', () => {
    const partial = run([
      { type: 'start', until_message_id: 'a-6', messages: 12, dropped_messages: 2 },
      { type: 'delta', text: 'Обсуждали договор' },
    ]);
    const stopped = draftReducer(partial, { type: 'stopped' });
    expect(stopped).toMatchObject({ phase: 'stopped', text: 'Обсуждали договор', until: 'a-6' });
    // Остановка после done ничего не меняет: черновик уже готов.
    const ready = run([{ type: 'done', truncated: false }], partial);
    expect(draftReducer(ready, { type: 'stopped' }).phase).toBe('ready');
    expect(draftReducer(stopped, { type: 'begin' })).toMatchObject({ phase: 'streaming', text: '', dropped: 0, until: undefined });
  });

  it('событие error — черновика нет, полученный текст остаётся для правки', () => {
    const state = run([
      { type: 'start', until_message_id: 'a-6', messages: 12, dropped_messages: 0 },
      { type: 'delta', text: 'Часть' },
      { type: 'error', code: 'model_unavailable', message: 'Модель недоступна.' },
    ]);
    expect(state.phase).toBe('failed');
    expect(state.text).toBe('Часть');
    const failure = state.failure;
    if (!failure) {
      throw new Error('нет отказа');
    }
    expect(draftNotice(failure, true, COMPRESS)).toEqual({ text: t.draft.broken, action: 'restart' });
    expect(draftNotice(failure, false, COMPRESS)).toEqual({ text: t.draft.failed, action: 'restart' });
  });
});

describe('черновик пересказа: отказы', () => {
  const refused = (status: number, code: string, fields: ApiError['fields'] = []) =>
    ({ kind: 'refused', error: new ApiError(status, { code, message: `Сообщение: ${code}`, fields }) }) as const;

  it('отказы до потока — тексты концепции, число «двух» — из memory.keep_recent_pairs', () => {
    expect(draftNotice(refused(409, 'nothing_to_summarize'), false, COMPRESS)?.text).toBe(
      'Сжимать пока нечего: последние два вопроса с ответами не сжимаются',
    );
    expect(draftNotice(refused(409, 'nothing_to_summarize'), false, { ...COMPRESS, keepRecent: 1 })?.text).toBe(
      'Сжимать пока нечего: последний вопрос с ответом не сжимается',
    );
    expect(draftNotice(refused(409, 'nothing_to_summarize'), false, TRANSFER)?.text).toBe(
      'Пересказывать нечего: модель не учитывает ни одного вопроса с ответом. Верните вопросы в панели памяти или выберите «Один ответ дословно»',
    );
    expect(draftNotice(refused(409, 'nothing_to_summarize'), false, { ...TRANSFER, verbatim: false })?.text).toBe(t.transfer.nothing);
    expect(draftNotice(refused(429, 'draft_in_progress'), false, TRANSFER)).toEqual({
      text: 'Вы уже готовите пересказ — в другом окне или вкладке. Дождитесь его или закройте то окно и повторите',
      action: 'retry',
    });
    expect(draftNotice(refused(409, 'generation_in_progress'), false, COMPRESS)?.text).toBe(t.changeBusy);
    expect(draftNotice(refused(422, 'message_too_long'), false, TRANSFER)?.text).toBe(t.transfer.inputTooLong + t.transfer.orVerbatim);
    const gone = refused(422, 'validation_error', [{ field: 'until_message_id', code: 'unknown_value', message: '' }]);
    expect(draftNotice(gone, false, { ...TRANSFER, verbatim: false })?.text).toBe(t.transfer.continueGone);
    expect(draftNotice({ kind: 'network' }, false, COMPRESS)).toEqual({ text: texts.common.noConnection, action: 'retry' });
  });

  it('модель занята — тот же текст, что у ответа в ленте; неизвестный код — текст сервера', () => {
    expect(draftNotice({ kind: 'event', code: 'model_overloaded', message: '' }, false, COMPRESS)).toEqual({
      text: texts.chat.errors.overloaded,
      action: 'restart',
    });
    expect(draftNotice({ kind: 'event', code: 'new_code', message: 'Текст сервера.' }, false, COMPRESS)?.text).toBe('Текст сервера.');
  });

  it('удаление диалога, выключенная функция и конец сессии закрывают окно без заметки', () => {
    expect(draftExit(refused(404, 'not_found'))).toBe('deleted');
    expect(draftExit({ kind: 'event', code: 'dialog_deleted', message: '' })).toBe('deleted');
    expect(draftExit(refused(403, 'feature_disabled'))).toBe('disabled');
    expect(draftExit({ kind: 'event', code: 'session_ended', message: '' })).toBe('session');
    expect(draftNotice(refused(403, 'feature_disabled'), false, COMPRESS)).toBeNull();
  });
});
