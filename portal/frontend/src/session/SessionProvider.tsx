import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { api, isApiError, onSessionSignal } from '../api/client';
import type { PortalConfig, Session } from '../api/types';
import { texts } from '../texts';
import { SessionContext, type LoginNotice, type SessionState } from './SessionContext';

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [status, setStatus] = useState<SessionState['status']>('loading');
  const [session, setSession] = useState<Session | null>(null);
  const [config, setConfig] = useState<PortalConfig | null>(null);
  const [loginNotice, setLoginNotice] = useState<LoginNotice | null>(null);
  const [returnTo, setReturnTo] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // Была ли сессия в этой загрузке приложения: только тогда её потеря — «Сеанс завершён» (§5.12).
  const hadSession = useRef(false);
  // Сотрудник вышел сам или сменил пароль: адрес для возврата после входа не запоминается.
  const leftVoluntarily = useRef(false);

  const applySession = useCallback((next: Session) => {
    hadSession.current = true;
    leftVoluntarily.current = false;
    setSession(next);
  }, []);

  const endSession = useCallback((notice?: LoginNotice, voluntary = false) => {
    hadSession.current = false;
    leftVoluntarily.current = voluntary;
    if (voluntary) {
      setReturnTo(null);
    }
    setSession(null);
    setConfig(null);
    setLoginNotice(notice ?? null);
  }, []);

  useEffect(
    () =>
      onSessionSignal((signal) => {
        if (signal === 'login_step_required') {
          // Шаг входа на сервере другой: перечитываем сессию, экран выберется по её шагу.
          api.getSession().then(applySession, () => {
            // Отказ `unauthenticated` уже обработан этим же слушателем; прочие сбои покажет экран, сделавший запрос.
          });
        } else if (hadSession.current) {
          endSession({ kind: 'info', text: texts.common.sessionEnded });
        } else {
          setSession(null);
        }
      }),
    [applySession, endSession],
  );

  useEffect(() => {
    let cancelled = false;
    api.getSession().then(
      (loaded) => {
        if (!cancelled) {
          applySession(loaded);
          setStatus('ready');
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setStatus(isApiError(error, 'unauthenticated') ? 'ready' : 'unavailable');
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [attempt, applySession]);

  const hasSession = session !== null;
  useEffect(() => {
    if (!hasSession) {
      return;
    }
    let cancelled = false;
    api.getConfig().then(
      (loaded) => {
        if (!cancelled) {
          setConfig(loaded);
        }
      },
      () => {
        // Без конфигурации подсказки обходятся без чисел, а тексты отказов приходят от сервера.
      },
    );
    return () => {
      cancelled = true;
    };
  }, [hasSession]);

  const logout = useCallback(async () => {
    // Собственный выход заметку «Сеанс завершён» не показывает (§5.12).
    hadSession.current = false;
    try {
      await api.logout();
    } catch (error) {
      if (!isApiError(error, 'unauthenticated')) {
        hadSession.current = true;
        throw error;
      }
    }
    endSession(undefined, true);
  }, [endSession]);

  const rememberReturnTo = useCallback((path: string) => {
    if (!leftVoluntarily.current) {
      setReturnTo(path);
    }
  }, []);

  const reload = useCallback(() => {
    setStatus('loading');
    setAttempt((value) => value + 1);
  }, []);

  const state = useMemo<SessionState>(
    () => ({
      status,
      session,
      config,
      loginNotice,
      returnTo,
      applySession,
      endSession,
      setLoginNotice,
      rememberReturnTo,
      logout,
      reload,
    }),
    [status, session, config, loginNotice, returnTo, applySession, endSession, rememberReturnTo, logout, reload],
  );

  return <SessionContext.Provider value={state}>{children}</SessionContext.Provider>;
}
