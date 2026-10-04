import { createContext, useContext } from 'react';

import type { PortalConfig, Session } from '../api/types';

export interface LoginNotice {
  kind: 'info' | 'error';
  text: string;
}

export interface SessionState {
  /** `loading` — идёт первый запрос сессии; `unavailable` — портал не ответил при открытии. */
  status: 'loading' | 'unavailable' | 'ready';
  session: Session | null;
  /** Конфигурация для подсказок (контракт §4); `null`, пока не получена. */
  config: PortalConfig | null;
  /** Заметка на экране входа: «Сеанс завершён», блокировка, истёкший шаг, смена пароля. */
  loginNotice: LoginNotice | null;
  /** Адрес, с которого сотрудника вернуло на вход; после входа открывается он. */
  returnTo: string | null;
  /** Принять `Session` из ответа сервера. */
  applySession: (session: Session) => void;
  /**
   * Сессии больше нет: перейти к входу с заметкой или без неё.
   * `voluntary` — сотрудник ушёл сам (выход, смена пароля): адрес для возврата не запоминается.
   */
  endSession: (notice?: LoginNotice, voluntary?: boolean) => void;
  setLoginNotice: (notice: LoginNotice | null) => void;
  rememberReturnTo: (path: string) => void;
  logout: () => Promise<void>;
  /** Повторить первый запрос сессии после «Портал не отвечает». */
  reload: () => void;
}

export const SessionContext = createContext<SessionState | null>(null);

export function useSession(): SessionState {
  const state = useContext(SessionContext);
  if (!state) {
    throw new Error('useSession вызван вне SessionProvider');
  }
  return state;
}

const STEP_PATHS = {
  second_factor: '/login/code',
  password_change: '/password',
  second_factor_setup: '/second-factor',
} as const;

/** Адрес экрана текущего шага входа (концепция §3.1); `null` — вход завершён. */
export function loginStepPath(session: Session | null): string | null {
  if (!session) {
    return '/login';
  }
  return session.step === 'ready' ? null : STEP_PATHS[session.step];
}
