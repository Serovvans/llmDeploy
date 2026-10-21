/**
 * Черновик пересказа (концепция §4.9, контракт §5.10.5, §6.5): состояние потока и тексты отказов.
 * Чистые функции — их проверяют модульные тесты; поток ведёт `useSummaryDraft`.
 */
import type { ApiError } from '../api/client';
import type { DraftEvent } from '../api/stream';
import { errorText, texts } from '../texts';

const t = texts.memory;

/**
 * `idle` — черновик не начат (перенос до «Подготовить пересказ»); `streaming` — идёт поток (и до `start`);
 * `ready` — `done`; `stopped` — остановлен кнопкой или обрывом связи; `failed` — отказ до потока или `error`.
 */
export type DraftPhase = 'idle' | 'streaming' | 'ready' | 'stopped' | 'failed';

/** Почему черновика нет: отказ до потока, нет связи до потока или событие `error` в потоке. */
export type DraftFailure =
  | { kind: 'refused'; error: ApiError }
  | { kind: 'network' }
  | { kind: 'event'; code: string; message: string };

export interface DraftState {
  phase: DraftPhase;
  text: string;
  /** Граница из `start`: её получает `PUT …/memory/summary`; `undefined` — `start` ещё не пришёл. */
  until: string | null | undefined;
  /** Сколько ранних сообщений не передано модели (`start.dropped_messages`): заметка «Вход урезан». */
  dropped: number;
  /** `done.truncated`: текст упёрся в предел длины. */
  truncated: boolean;
  failure: DraftFailure | null;
}

export const IDLE_DRAFT: DraftState = { phase: 'idle', text: '', until: undefined, dropped: 0, truncated: false, failure: null };

export type DraftAction =
  | { type: 'begin' }
  | { type: 'event'; event: DraftEvent }
  /** Остановлен кнопкой или обрывом потока после `200`: полученный текст остаётся. */
  | { type: 'stopped' }
  | { type: 'failed'; failure: DraftFailure }
  | { type: 'edited'; text: string }
  /** Текст поставлен в поле без модели: «Изменить» пересказ или «Один ответ дословно». */
  | { type: 'loaded'; text: string; until: string | null }
  /** Черновика снова нет: перенос вернулся к «Подготовить пересказ». */
  | { type: 'reset' };

export function draftReducer(state: DraftState, action: DraftAction): DraftState {
  switch (action.type) {
    case 'begin':
      // Новый `start` решает заново и про заметку «Вход урезан» (§4.9).
      return { ...IDLE_DRAFT, phase: 'streaming' };
    case 'event': {
      const event = action.event;
      switch (event.type) {
        case 'start':
          return { ...state, until: event.until_message_id, dropped: event.dropped_messages };
        case 'delta':
          return { ...state, text: state.text + event.text };
        case 'done':
          return { ...state, phase: 'ready', truncated: event.truncated };
        case 'error':
          return { ...state, phase: 'failed', failure: { kind: 'event', code: event.code, message: event.message } };
      }
      return state;
    }
    case 'stopped':
      return state.phase === 'streaming' ? { ...state, phase: 'stopped' } : state;
    case 'failed':
      return { ...state, phase: 'failed', failure: action.failure };
    case 'edited':
      return { ...state, text: action.text };
    case 'loaded':
      return { ...IDLE_DRAFT, phase: 'ready', text: action.text, until: action.until };
    case 'reset':
      return IDLE_DRAFT;
  }
}

/** Что сделать с окном при отказе, кроме заметки: диалог удалён, функция выключена, сессия кончилась. */
export type DraftExit = 'deleted' | 'disabled' | 'session';

export function draftExit(failure: DraftFailure): DraftExit | null {
  if (failure.kind === 'refused') {
    if (failure.error.code === 'not_found') {
      return 'deleted';
    }
    if (failure.error.code === 'feature_disabled') {
      return 'disabled';
    }
    return failure.error.code === 'unauthenticated' ? 'session' : null;
  }
  if (failure.kind === 'event') {
    if (failure.code === 'dialog_deleted') {
      return 'deleted';
    }
    return failure.code === 'session_ended' ? 'session' : null;
  }
  return null;
}

export interface DraftContext {
  purpose: 'compress' | 'transfer';
  /** `memory.keep_recent_pairs` — число в тексте «Сжимать пока нечего…». */
  keepRecent: number;
  /** В окне переноса есть «Один ответ дословно»: тексты отказов предлагают его. */
  verbatim: boolean;
}

/** `action`: `restart` — «Подготовить заново», `retry` — «Повторить» (тот же новый черновик). */
export interface DraftNotice {
  text: string;
  action: 'restart' | 'retry' | null;
}

function isFieldError(error: ApiError, field: string, code: string): boolean {
  return error.fields.some((item) => item.field === field && item.code === code);
}

/** Текст заметки над полем по отказу черновика (концепция §4.9, «Ошибки сжатия и переноса»); `null` — заметки нет. */
export function draftNotice(failure: DraftFailure, hasText: boolean, context: DraftContext): DraftNotice | null {
  if (draftExit(failure)) {
    return null;
  }
  const tooLong =
    context.purpose === 'compress'
      ? t.compress.inputTooLong
      : t.transfer.inputTooLong + (context.verbatim ? t.transfer.orVerbatim : '');
  if (failure.kind === 'network') {
    return { text: texts.common.noConnection, action: 'retry' };
  }
  if (failure.kind === 'refused') {
    const { error } = failure;
    switch (error.code) {
      case 'generation_in_progress':
        return { text: t.changeBusy, action: null };
      case 'nothing_to_summarize':
        return context.purpose === 'compress'
          ? { text: t.summary.nothing(context.keepRecent), action: null }
          : { text: t.transfer.nothing + (context.verbatim ? t.transfer.orVerbatim : ''), action: null };
      case 'message_too_long':
        return { text: tooLong, action: null };
      case 'draft_in_progress':
        return { text: t.draft.draftInProgress, action: 'retry' };
    }
    if (error.code === 'validation_error' && isFieldError(error, 'until_message_id', 'unknown_value')) {
      return { text: t.transfer.continueGone, action: null };
    }
    return { text: errorText(error), action: 'retry' };
  }
  switch (failure.code) {
    case 'model_overloaded':
      return { text: t.draft.overloaded, action: 'restart' };
    case 'message_too_long':
      return { text: tooLong, action: null };
    case 'model_unavailable':
    case 'generation_timeout':
    case 'internal_error':
      return { text: hasText ? t.draft.broken : t.draft.failed, action: 'restart' };
  }
  // Кода нет в словаре: текст сервера (§8.3).
  return { text: failure.message || (hasText ? t.draft.broken : t.draft.failed), action: 'restart' };
}

/** Текст в поле во время потока — с чертой в конце, как ответ в ленте (§4.9). */
export const STREAM_CARET = '▍';
