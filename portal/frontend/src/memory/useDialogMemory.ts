import { SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api, isApiError, NetworkError } from '../api/client';
import type { DialogKind, Memory, MemoryLevel, MemoryMode, MemoryPair, PortalConfig, QuestionParams } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';

const t = texts.memory;
/** Смена настроек следующего вопроса пересчитывается не на каждую отметку (концепция §4.9). */
export const REESTIMATE_DELAY_MS = 300;

/**
 * Строка запроса оценки — то, с чем уйдёт следующий вопрос (контракт §5.10.3): в чате режим и есть ли
 * база знаний, в SQL действие, диалект и схема (проверка на тестовых данных место не меняет), в CoGIS действие,
 * у помощника проекта — действие и видимые файлы (контракт §7.5.8: нет — все, пустая строка — ни одного).
 */
export function memoryQuery(params: QuestionParams): Record<string, string> {
  if ('file_ids' in params) {
    return { action: params.action, ...(params.file_ids ? { files: params.file_ids.join(',') } : {}) };
  }
  if ('mode' in params) {
    return { mode: params.mode, search: String(params.collections.length > 0) };
  }
  if ('dialect' in params) {
    return { action: params.action, dialect: params.dialect, ...(params.schema_id ? { schema_id: params.schema_id } : {}) };
  }
  if ('action' in params) {
    return { action: params.action };
  }
  return {};
}

/**
 * Строка запроса оценки для диалога, открытого не на экране (перенос из строки истории, концепция §4.9):
 * чат — быстрый ответ без поиска, SQL — первое действие и диалект по умолчанию без схемы, CoGIS — первое действие.
 */
export function defaultMemoryQuery(kind: DialogKind, config: PortalConfig): Record<string, string> {
  switch (kind) {
    case 'chat':
      return { mode: 'fast', search: 'false' };
    case 'sql':
      return { action: 'write', dialect: config.sql.default_dialect };
    case 'cogis':
    case 'code':
      return { action: 'write' };
    case 'docparse':
      return {};
  }
}

/**
 * Почему запрошена оценка: от этого зависит, объявляется ли новый уровень (концепция §9). `summary` —
 * сотрудник сжал раннюю часть или отменил сжатие; `stale` — оценка устарела (сжимать уже нечего).
 */
type Reason = 'open' | 'settings' | 'answer' | 'choice' | 'summary' | 'stale';

const HIGH: ReadonlySet<MemoryLevel> = new Set(['almost_full', 'overflow']);

/** Объявление уровня: после выбора в описи — всегда, после ответа — только при переходе в высокий уровень. */
function announcement(reason: Reason, previous: MemoryLevel | null, next: MemoryLevel): string | null {
  if (reason === 'choice' || reason === 'summary' || (reason === 'answer' && HIGH.has(next) && previous !== next)) {
    return t.announce(t.levels[next]);
  }
  return null;
}

export type MemoryStatus = 'new' | 'loading' | 'ready' | 'failed';

export interface DialogMemory {
  /** Функция включена и не выключена по ходу работы (`403 feature_disabled`). */
  active: boolean;
  /** `new` — диалог ещё не создан на сервере: «свободно» без запроса. */
  status: MemoryStatus;
  memory: Memory | null;
  /** Уровень на экране: событие `context_truncated` сразу даёт «переполнена», до новой оценки. */
  level: MemoryLevel | null;
  /** Уровень поднят событием потока, а не оценкой: процента для подсказки нет. */
  truncated: boolean;
  /** Ошибки выбора у строк описи — по идентификатору вопроса. */
  rowErrors: Record<string, string>;
  announcement: string;
  retry: () => void;
  setMode: (pair: MemoryPair, mode: MemoryMode) => void;
  markTruncated: () => void;
  /** Новая оценка после сжатия, правки или отмены (0.8) — уровень объявляется; `stale` — без объявления. */
  refresh: (reason: 'summary' | 'stale') => void;
  /** Функцию выключили, пока экран открыт (`403 feature_disabled` у окна сжатия или переноса). */
  disable: () => void;
}

interface Options {
  kind: DialogKind;
  /** `null` — новый диалог, ещё не созданный на сервере. */
  dialogId: string | null;
  /** Лента диалога загружена: у «не найден» и сбоя загрузки оценки нет. */
  ready: boolean;
  enabled: boolean;
  params: QuestionParams;
  /** В диалоге формируется ответ — здесь или в другой вкладке. */
  busy: boolean;
  /** Схема из строки запроса удалена в другой вкладке (`422 schema_not_found`). */
  onSchemaMissing?: () => void;
  /** Помощник проекта: меняется после каждого сохранённого изменения файлов — оценка повторяется через 300 мс. */
  revision?: string;
}

interface Estimate {
  dialogId: string;
  memory: Memory | null;
  failed: boolean;
}

/**
 * Оценка памяти диалога — одна на индикатор и панель (концепция §4.9, API-26): уровень берётся
 * из ответа сервера целиком. Обновляется при открытии диалога, по завершении ответа, после выбора
 * в описи и через 300 мс после смены настроек следующего вопроса.
 */
export function useDialogMemory({
  kind,
  dialogId,
  ready,
  enabled,
  params,
  busy,
  onSchemaMissing,
  revision = '',
}: Options): DialogMemory {
  const chat = useChat();
  const { reloadConfig } = useSession();
  const { forgetDialog, showMemoryMode } = chat;
  const [estimate, setEstimate] = useState<Estimate | null>(null);
  const [disabled, setDisabled] = useState(false);
  /** Событие `context_truncated`: в каком диалоге и после какого запроса оценки. */
  const [truncation, setTruncation] = useState<{ dialogId: string; after: number } | null>(null);
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  // Ошибки у строк описи относятся к моменту: они уходят при смене диалога и по окончании ответа.
  const [errorsFor, setErrorsFor] = useState({ dialogId, busy });
  if (errorsFor.dialogId !== dialogId || errorsFor.busy !== busy) {
    setErrorsFor({ dialogId, busy });
    if (errorsFor.dialogId !== dialogId || !busy) {
      setRowErrors({});
    }
  }
  const [announced, setAnnounced] = useState('');
  const sequence = useRef(0);
  const current = useRef({ dialogId, query: memoryQuery(params), onSchemaMissing });
  const loadedDialog = useRef<string | null>(null);
  const lastLevel = useRef<{ dialogId: string; level: MemoryLevel } | null>(null);
  const toastShown = useRef(false);
  const active = enabled && !disabled;
  const query = memoryQuery(params);
  const queryKey = JSON.stringify([query, revision]);

  useEffect(() => {
    current.current = { dialogId, query, onSchemaMissing };
  });

  /** Функцию выключили, пока экран открыт: всё памяти уходит с экрана, тост — один раз. */
  const disable = useCallback(() => {
    setDisabled(true);
    reloadConfig();
    if (!toastShown.current) {
      toastShown.current = true;
      SingleToast.push(t.disabled);
    }
    // Фокус был в панели — он переходит в поле ввода, а не теряется вместе с панелью.
    if (document.activeElement?.closest('[data-memory-panel]')) {
      document.querySelector<HTMLElement>('[data-composer] textarea')?.focus();
    }
  }, [reloadConfig]);

  const deleted = useCallback(
    (id: string) => {
      forgetDialog(id);
      SingleToast.push(texts.dialogs[kind].removedElsewhere);
    },
    [forgetDialog, kind],
  );

  const load = useCallback(
    (reason: Reason) => {
      const id = current.current.dialogId;
      if (!id) {
        return;
      }
      loadedDialog.current = id;
      const seq = (sequence.current += 1);
      api.getMemory(id, current.current.query).then(
        (memory) => {
          if (seq !== sequence.current) {
            return;
          }
          const before = lastLevel.current?.dialogId === id ? lastLevel.current.level : null;
          lastLevel.current = { dialogId: id, level: memory.level };
          const text = announcement(reason, before, memory.level);
          if (text) {
            setAnnounced(text);
          }
          setEstimate({ dialogId: id, memory, failed: false });
          setRowErrors({});
          setTruncation((value) => (value && value.after < seq ? null : value));
        },
        (error: unknown) => {
          if (seq !== sequence.current) {
            return;
          }
          if (isApiError(error, 'feature_disabled')) {
            disable();
          } else if (isApiError(error, 'not_found')) {
            deleted(id);
          } else if (isApiError(error, 'schema_not_found') && current.current.onSchemaMissing) {
            // Экран обновит список схем: выбор станет «Без схемы», и оценка повторится уже без неё.
            current.current.onSchemaMissing();
          } else if (!isApiError(error, 'unauthenticated')) {
            setEstimate({ dialogId: id, memory: null, failed: true });
          }
        },
      );
    },
    [deleted, disable],
  );

  // Открытие диалога — сразу; смена настроек следующего вопроса — через 300 мс после последней.
  useEffect(() => {
    if (!active || !dialogId || !ready) {
      return;
    }
    if (loadedDialog.current !== dialogId) {
      load('open');
      return;
    }
    const timer = window.setTimeout(() => load('settings'), REESTIMATE_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [active, dialogId, ready, queryKey, load]);

  // Ответ закончился (готов, остановлен, ошибка) — здесь или в другой вкладке.
  const wasBusy = useRef(busy);
  useEffect(() => {
    if (wasBusy.current && !busy && active && ready) {
      load('answer');
    }
    wasBusy.current = busy;
  }, [busy, active, ready, load]);

  const setMode = useCallback(
    (pair: MemoryPair, mode: MemoryMode) => {
      const id = current.current.dialogId;
      if (!id || mode === pair.memory_mode) {
        return;
      }
      const previous = pair.memory_mode;
      const show = (value: MemoryMode) => {
        showMemoryMode(id, pair.question_id, value);
        setEstimate((state) =>
          state?.dialogId === id && state.memory
            ? {
                ...state,
                memory: {
                  ...state.memory,
                  pairs: state.memory.pairs.map((item) =>
                    item.question_id === pair.question_id ? { ...item, memory_mode: value } : item,
                  ),
                },
              }
            : state,
        );
      };
      const setRowError = (text: string | null) =>
        setRowErrors((errors) => {
          const rest = Object.fromEntries(Object.entries(errors).filter(([key]) => key !== pair.question_id));
          return text ? { ...rest, [pair.question_id]: text } : rest;
        });
      // Выбор встаёт на место сразу; уровень — после ответа сервера (до него — прежний).
      // Оценка, запрошенная раньше выбора, его бы затёрла: её ответ больше не применяется.
      sequence.current += 1;
      setRowError(null);
      show(mode);
      api.setMemoryMode(id, pair.question_id, mode).then(
        () => load('choice'),
        (error: unknown) => {
          show(previous);
          if (isApiError(error, 'feature_disabled')) {
            disable();
          } else if (isApiError(error, 'not_found')) {
            deleted(id);
          } else if (isApiError(error, 'generation_in_progress')) {
            setRowError(t.changeBusy);
          } else if (error instanceof NetworkError) {
            setRowError(texts.common.noConnection);
          } else if (!isApiError(error, 'unauthenticated')) {
            setRowError(t.changeFailed);
          }
        },
      );
    },
    [deleted, disable, load, showMemoryMode],
  );

  const markTruncated = useCallback(() => {
    const id = current.current.dialogId;
    if (id) {
      setTruncation({ dialogId: id, after: sequence.current });
    }
  }, []);

  const retry = useCallback(() => {
    setEstimate(null);
    load('open');
  }, [load]);

  const refresh = useCallback((reason: 'summary' | 'stale') => load(reason), [load]);

  const own = estimate?.dialogId === dialogId ? estimate : null;
  let status: MemoryStatus = 'loading';
  if (!dialogId) {
    status = 'new';
  } else if (own?.failed) {
    status = 'failed';
  } else if (own?.memory) {
    status = 'ready';
  }
  const memory = status === 'ready' ? (own?.memory ?? null) : null;
  const truncated = truncation !== null && truncation.dialogId === dialogId;
  let level: MemoryLevel | null = memory?.level ?? null;
  if (status === 'new') {
    level = 'free';
  } else if (truncated) {
    level = 'overflow';
  }
  return {
    active,
    status,
    memory,
    level,
    truncated,
    rowErrors,
    announcement: announced,
    retry,
    setMode,
    markTruncated,
    refresh,
    disable,
  };
}
