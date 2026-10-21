import { useCallback, useEffect, useReducer, useRef } from 'react';

import { api, ApiError, isAbort, NetworkError, StreamBrokenError } from '../api/client';
import { draftExit, draftReducer, IDLE_DRAFT, type DraftExit, type DraftState } from './draft';

type DraftBody = { purpose: 'compress' } | { purpose: 'transfer'; until_message_id?: string | null };

export interface SummaryDraft {
  state: DraftState;
  /** Новый черновик: прежний поток закрывается, текст и заметки начинаются заново. */
  start: (body: DraftBody) => void;
  /** «Остановить»: полученный текст остаётся для правки. */
  stop: () => void;
  edit: (text: string) => void;
  /** Текст без модели: «Изменить» пересказ, «Один ответ дословно». Идущий поток закрывается. */
  load: (text: string, until: string | null) => void;
  /** Черновика снова нет (перенос вернулся к «Подготовить пересказ»); идущий поток закрывается. */
  reset: () => void;
}

/**
 * Поток черновика пересказа (контракт §6.5). Закрытие окна закрывает поток — это отмена: незаконченный
 * черновик никуда не записан (ПД §5.7). `onExit` — диалог удалён, функция выключена или сессия кончилась.
 */
export function useSummaryDraft(dialogId: string, onExit: (exit: DraftExit) => void): SummaryDraft {
  const [state, dispatch] = useReducer(draftReducer, IDLE_DRAFT);
  const controller = useRef<AbortController | null>(null);
  const exit = useRef(onExit);

  useEffect(() => {
    exit.current = onExit;
  });

  const close = useCallback(() => {
    controller.current?.abort();
    controller.current = null;
  }, []);

  useEffect(() => close, [close]);

  const start = useCallback(
    (body: DraftBody) => {
      close();
      const abort = new AbortController();
      controller.current = abort;
      dispatch({ type: 'begin' });
      void (async () => {
        try {
          for await (const event of api.draftSummary(dialogId, body, abort.signal)) {
            dispatch({ type: 'event', event });
            if (event.type === 'error') {
              const gone = draftExit({ kind: 'event', code: event.code, message: event.message });
              if (gone) {
                exit.current(gone);
              }
            }
          }
        } catch (error) {
          if (controller.current !== abort) {
            // Черновик заменён новым или окно закрыто: этот поток больше никому не нужен.
            return;
          }
          if (isAbort(error) || error instanceof StreamBrokenError) {
            // Остановка кнопкой и обрыв связи после `200` выглядят одинаково (§4.9, «Остановлено»).
            dispatch({ type: 'stopped' });
          } else if (error instanceof ApiError) {
            dispatch({ type: 'failed', failure: { kind: 'refused', error } });
            const gone = draftExit({ kind: 'refused', error });
            if (gone) {
              exit.current(gone);
            }
          } else if (error instanceof NetworkError) {
            dispatch({ type: 'failed', failure: { kind: 'network' } });
          } else {
            // Неизвестное исключение: без этого поле осталось бы в «streaming» и только для чтения.
            dispatch({ type: 'failed', failure: { kind: 'event', code: 'internal_error', message: '' } });
          }
        } finally {
          if (controller.current === abort) {
            controller.current = null;
          }
        }
      })();
    },
    [close, dialogId],
  );

  const stop = useCallback(() => controller.current?.abort(), []);

  const edit = useCallback((text: string) => dispatch({ type: 'edited', text }), []);

  const load = useCallback(
    (text: string, until: string | null) => {
      close();
      dispatch({ type: 'loaded', text, until });
    },
    [close],
  );

  const reset = useCallback(() => {
    close();
    dispatch({ type: 'reset' });
  }, [close]);

  return { state, start, stop, edit, load, reset };
}
