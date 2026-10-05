import { Loader, SingleToast } from '@skbkontur/react-ui';
import { useEffect } from 'react';
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from 'react-router-dom';

import { ChatProvider } from './chat/ChatProvider';
import { AppShell } from './components/AppShell';
import { ChatScreen } from './screens/ChatScreen';
import { CodeScreen } from './screens/CodeScreen';
import { LoginScreen } from './screens/LoginScreen';
import { PasswordScreen } from './screens/PasswordScreen';
import { ProfileScreen } from './screens/ProfileScreen';
import { SecondFactorScreen } from './screens/SecondFactorScreen';
import { SectionStub } from './screens/SectionStub';
import { CrashBoundary, NotFoundScreen, UnavailableScreen } from './screens/ServiceScreens';
import { UsersScreen } from './screens/UsersScreen';
import { loginStepPath, useSession } from './session/SessionContext';
import { SessionProvider } from './session/SessionProvider';
import { texts } from './texts';
import { ThemeProvider } from './theme/ThemeProvider';

const HOME = '/chat';

/** Экраны входа: адрес, не совпадающий с текущим шагом, ведёт на экран этого шага (концепция §3.1). */
function LoginFlow() {
  const { session, returnTo } = useSession();
  const { pathname } = useLocation();
  const stepPath = loginStepPath(session);

  if (stepPath === null) {
    return <Navigate to={returnTo ?? HOME} replace />;
  }
  return pathname === stepPath ? <Outlet /> : <Navigate to={stepPath} replace />;
}

/** Переход на экран шага входа с запоминанием адреса, на который сотрудник вернётся после входа. */
function ToLoginStep({ to }: { to: string }) {
  const { rememberReturnTo } = useSession();
  const { pathname, search } = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    if (pathname !== '/') {
      rememberReturnTo(pathname + search);
    }
    navigate(to, { replace: true });
  }, [navigate, pathname, rememberReturnTo, search, to]);

  return null;
}

/** Рабочие экраны: доступны только при завершённом входе (шаг `ready`). */
function Workspace() {
  const { session, config } = useSession();
  const stepPath = loginStepPath(session);

  if (stepPath !== null || !session?.user) {
    return <ToLoginStep to={stepPath ?? '/login'} />;
  }
  const user = session.user;
  return (
    <ChatProvider config={config}>
      <AppShell user={user}>
        <Routes>
        <Route path="/" element={<Navigate to={HOME} replace />} />
        <Route path="/chat/:id?" element={<ChatScreen />} />
        <Route path="/knowledge" element={<SectionStub section="knowledge" />} />
        <Route path="/sql/:id?" element={<SectionStub section="sql" />} />
        <Route path="/cogis/:id?" element={<SectionStub section="cogis" />} />
        <Route path="/documents/:id?" element={<SectionStub section="documents" />} />
        <Route path="/admin/users" element={<UsersScreen />} />
        <Route path="/profile" element={<ProfileScreen user={user} />} />
        <Route path="*" element={<NotFoundScreen />} />
        </Routes>
      </AppShell>
    </ChatProvider>
  );
}

function Screens() {
  const { status, reload } = useSession();

  if (status === 'loading') {
    return (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div style={{ height: '100vh' }} />
      </Loader>
    );
  }
  if (status === 'unavailable') {
    return <UnavailableScreen onRetry={reload} />;
  }
  return (
    <Routes>
      <Route element={<LoginFlow />}>
        <Route path="/login" element={<LoginScreen />} />
        <Route path="/login/code" element={<CodeScreen />} />
        <Route path="/password" element={<PasswordScreen />} />
        <Route path="/second-factor" element={<SecondFactorScreen />} />
      </Route>
      <Route path="*" element={<Workspace />} />
    </Routes>
  );
}

export function App() {
  return (
    <ThemeProvider>
      <CrashBoundary>
        <SessionProvider>
          <Screens />
        </SessionProvider>
      </CrashBoundary>
      <SingleToast />
    </ThemeProvider>
  );
}
