import { Button, PasswordInput } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import { AuthLayout } from '../components/AuthLayout';
import { Notice } from '../components/Notice';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import styles from './AuthForm.module.css';
import { checkNewPassword, NO_PASSWORD_ERRORS, passwordErrorsFrom } from './authErrors';
import { NewPasswordFields } from './NewPasswordFields';

/** Обязательная смена временного пароля (концепция §5.3). */
export function PasswordScreen() {
  usePageTitle(texts.password.title);
  const { session, config, applySession, endSession } = useSession();
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [errors, setErrors] = useState(NO_PASSWORD_ERRORS);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const nextRef = useRef<PasswordInput>(null);
  const repeatRef = useRef<PasswordInput>(null);
  const limits = config?.password ?? null;

  useEffect(() => {
    if (errors.next) {
      nextRef.current?.focus();
    } else if (errors.repeat) {
      repeatRef.current?.focus();
    }
  }, [errors]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    const checked = checkNewPassword(next, repeat, limits);
    setErrors(checked);
    setNotice(null);
    if (checked.next || checked.repeat) {
      return;
    }
    setSubmitting(true);
    try {
      applySession(await api.changeTemporaryPassword(next));
    } catch (error) {
      const fieldErrors = passwordErrorsFrom(error, limits);
      if (fieldErrors) {
        setErrors(fieldErrors);
      } else if (isApiError(error, 'login_step_expired')) {
        endSession({ kind: 'error', text: texts.login.loginStepExpired });
      } else if (!isApiError(error, 'unauthenticated') && !isApiError(error, 'login_step_required')) {
        setNotice(errorText(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <AuthLayout title={texts.password.title}>
      <p>{session?.user?.second_factor_configured ? texts.password.introReset : texts.password.introFirst}</p>
      <form className={styles.form} onSubmit={submit} noValidate>
        {notice && <Notice kind="error">{notice}</Notice>}
        <NewPasswordFields
          next={next}
          repeat={repeat}
          onNextChange={setNext}
          onRepeatChange={setRepeat}
          nextError={errors.next}
          repeatError={errors.repeat}
          minLength={limits?.min_length ?? null}
          disabled={submitting}
          size="large"
          autoFocus
          nextRef={nextRef}
          repeatRef={repeatRef}
        />
        <Button type="submit" use="primary" size="large" width="100%" loading={submitting}>
          {texts.password.submit}
        </Button>
      </form>
    </AuthLayout>
  );
}
