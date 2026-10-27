import { Loader, SingleToast } from '@skbkontur/react-ui';
import { lazy, Suspense, useEffect } from 'react';
import { Navigate, Outlet, Route, Routes, useLocation, useNavigate, useParams } from 'react-router-dom';

import { DatabasesScreen } from './admin/DatabasesScreen';
import { ChatProvider } from './chat/ChatProvider';
import { MediaUploadsProvider } from './chat/MediaUploads';
import { DocparseProvider } from './docparse/DocparseProvider';
import { AppShell } from './components/AppShell';
import { ConfigNotice } from './components/ConfigNotice';
import { ChatScreen } from './screens/ChatScreen';
import { CodeScreen } from './screens/CodeScreen';
import { CogisScreen } from './screens/CogisScreen';
import { DocumentsScreen } from './screens/DocumentsScreen';
import { KnowledgeScreen } from './screens/KnowledgeScreen';
import { LoginScreen } from './screens/LoginScreen';
import { PasswordScreen } from './screens/PasswordScreen';
import { ProfileScreen } from './screens/ProfileScreen';
import { SecondFactorScreen } from './screens/SecondFactorScreen';
import { SqlScreen } from './screens/SqlScreen';
import { CrashBoundary, NotFoundScreen, UnavailableScreen } from './screens/ServiceScreens';
import { UsersScreen } from './screens/UsersScreen';
import { loginStepPath, useSession } from './session/SessionContext';
import { SessionProvider } from './session/SessionProvider';
import { texts } from './texts';
import { ThemeProvider } from './theme/ThemeProvider';

const HOME = '/chat';

const CodeSection = lazy(() => import('./code/CodeSection'));

/**
 * Флаг раздела «Код» (концепция §5.13.14): включён — `/cogis…` ведёт в раздел, прежний диалог открывается
 * на `/code/dialogs/:id`; выключен — `/code…` ведёт на прежний экран помощника CoGIS.
 */
function CogisRoute() {
  const { config } = useSession();
  const { id } = useParams();
  // Until the flag is known neither screen is mounted: the old one would fetch its dialogs and flash.
  if (!config) {
    return <ConfigNotice />;
  }
  if (config.code.enabled) {
    return <Navigate to={id ? `/code/dialogs/${id}` : '/code'} replace />;
  }
  return <CogisScreen />;
}

function CodeRoute() {
  const { config } = useSession();
  const { pathname } = useLocation();
  if (!config) {
    return null;
  }
  if (!config.code.enabled) {
    const old = /^\/code\/dialogs\/([^/]+)/.exec(pathname)?.[1];
    return <Navigate to={old ? `/cogis/${old}` : '/cogis'} replace />;
  }
  return (
    <Suspense
      fallback={
        <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
          <div style={{ flex: 1 }} />
        </Loader>
      }
    >
      <CodeSection />
    </Suspense>
  );
}

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
    <MediaUploadsProvider>
    <ChatProvider config={config}>
      <DocparseProvider config={config}>
      <AppShell user={user}>
        <Routes>
        <Route path="/" element={<Navigate to={HOME} replace />} />
        <Route path="/chat/:id?" element={<ChatScreen />} />
        <Route path="/knowledge" element={<KnowledgeScreen />} />
        <Route path="/sql/:id?" element={<SqlScreen />} />
        <Route path="/cogis/:id?" element={<CogisRoute />} />
        <Route path="/code/*" element={<CodeRoute />} />
        <Route path="/documents/:id?" element={<DocumentsScreen />} />
        <Route path="/admin/users" element={<UsersScreen />} />
        <Route path="/admin/databases" element={<DatabasesScreen />} />
        <Route path="/profile" element={<ProfileScreen user={user} />} />
        <Route path="*" element={<NotFoundScreen />} />
        </Routes>
      </AppShell>
      </DocparseProvider>
    </ChatProvider>
    </MediaUploadsProvider>
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
