import { Button, Input, PasswordInput } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import { AuthLayout } from '../components/AuthLayout';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import styles from './AuthForm.module.css';
import { loginErrorText } from './authErrors';

type FieldName = 'login' | 'password';

export function LoginScreen() {
  usePageTitle(texts.product);
  const { loginNotice, setLoginNotice, applySession } = useSession();
  const [login, setLogin] = useState('');
  const [password, setPassword] = useState('');
  const [errors, setErrors] = useState<Record<FieldName, string | null>>({ login: null, password: null });
  const [submitting, setSubmitting] = useState(false);
  const [focusRequest, setFocusRequest] = useState<{ field: FieldName } | null>(null);
  const loginRef = useRef<Input>(null);
  const passwordRef = useRef<PasswordInput>(null);

  // Фокус ставится после того, как поля снова стали доступны.
  useEffect(() => {
    if (focusRequest?.field === 'login') {
      loginRef.current?.focus();
    } else if (focusRequest?.field === 'password') {
      passwordRef.current?.focus();
    }
  }, [focusRequest]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    const nextErrors = {
      login: login.trim() ? null : texts.login.enterLogin,
      password: password ? null : texts.login.enterPassword,
    };
    setErrors(nextErrors);
    if (nextErrors.login || nextErrors.password) {
      setFocusRequest({ field: nextErrors.login ? 'login' : 'password' });
      return;
    }

    setLoginNotice(null);
    setSubmitting(true);
    try {
      applySession(await api.login(login.trim(), password));
    } catch (error) {
      setLoginNotice({ kind: 'error', text: loginErrorText(error) });
      if (isApiError(error, 'invalid_credentials')) {
        setPassword('');
        setFocusRequest({ field: 'password' });
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthLayout title={texts.product}>
      <form className={styles.form} onSubmit={submit} noValidate>
        {loginNotice && <Notice kind={loginNotice.kind}>{loginNotice.text}</Notice>}
        <Field label={texts.login.loginLabel} error={errors.login}>
          {(control) => (
            <Input
              {...control}
              ref={loginRef}
              size="large"
              width="100%"
              autoComplete="username"
              autoFocus
              value={login}
              onValueChange={setLogin}
              disabled={submitting}
            />
          )}
        </Field>
        <Field label={texts.login.passwordLabel} error={errors.password}>
          {(control) => (
            <PasswordInput
              {...control}
              ref={passwordRef}
              size="large"
              width="100%"
              autoComplete="current-password"
              detectCapsLock
              value={password}
              onValueChange={setPassword}
              disabled={submitting}
            />
          )}
        </Field>
        <Button type="submit" use="primary" size="large" width="100%" loading={submitting}>
          {texts.login.submit}
        </Button>
        <p className={styles.help}>{texts.login.help}</p>
      </form>
    </AuthLayout>
  );
}
