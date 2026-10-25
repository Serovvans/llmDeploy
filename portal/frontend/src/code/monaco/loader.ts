import { useEffect, useState } from 'react';

/** Модуль Monaco грузится отдельным файлом и один раз — только в разделе «Код» (правило 13). */
type Setup = typeof import('./setup');

let pending: Promise<Setup> | null = null;
let loaded: Setup | null = null;

export function loadMonaco(): Promise<Setup> {
  if (!pending) {
    pending = import('./setup').then(
      (module) => {
        loaded = module;
        return module;
      },
      (error: unknown) => {
        // Следующая попытка — после «Обновить страницу»; повтор в той же странице не поможет.
        pending = null;
        throw error;
      },
    );
  }
  return pending;
}

export type MonacoState = { status: 'loading' } | { status: 'ready'; setup: Setup } | { status: 'failed' };

/** Состояние загрузки редактора (концепция §5.13.6: «Загрузка редактора…» / «Не удалось загрузить редактор»). */
export function useMonaco(): MonacoState {
  const [state, setState] = useState<MonacoState>(() => (loaded ? { status: 'ready', setup: loaded } : { status: 'loading' }));
  useEffect(() => {
    if (loaded) {
      return;
    }
    let cancelled = false;
    loadMonaco().then(
      (setup) => {
        if (!cancelled) {
          setState({ status: 'ready', setup });
        }
      },
      () => {
        if (!cancelled) {
          setState({ status: 'failed' });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);
  return state;
}
