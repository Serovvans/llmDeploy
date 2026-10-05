import { Button, Input, Link, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import { AuthLayout } from '../components/AuthLayout';
import { checkCode, CodeInput, normalizeCode, useAutoSubmitOnce } from '../components/CodeInput';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import styles from './AuthForm.module.css';
import { loginLockedText, tooManyAttemptsText } from './authErrors';

type Mode = 'app' | 'backup';

const FIELD_ERRORS: Record<string, string> = {
  invalid_code: texts.code.invalidCode,
  code_already_used: texts.code.codeAlreadyUsed,
  invalid_backup_code: texts.code.invalidBackupCode,
};

export function CodeScreen() {
  const [mode, setMode] = useState<Mode>('app');
  const title = mode === 'app' ? texts.code.title : texts.code.backupTitle;
  usePageTitle(title);
  const { applySession, endSession, logout } = useSession();
  const [value, setValue] = useState('');
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [focusTick, setFocusTick] = useState(0);
  const inputRef = useRef<Input>(null);

  const shouldAutoSubmit = useAutoSubmitOnce();

  // После ошибки содержимое поля выделяется: новый код набирается поверх.
  useEffect(() => {
    if (focusTick > 0) {
      inputRef.current?.focus();
      inputRef.current?.selectAll();
    }
  }, [focusTick]);

  const send = async (entered: string) => {
    if (submitting) {
      return;
    }
    const invalid = mode === 'app' ? checkCode(entered) : entered.trim() ? null : texts.code.enterBackupCode;
    setFieldError(invalid);
    if (invalid) {
      setFocusTick((tick) => tick + 1);
      return;
    }
    setNotice(null);
    setSubmitting(true);
    try {
      const result = await (mode === 'app' ? api.submitCode(normalizeCode(entered)) : api.submitBackupCode(entered));
      const remaining = result.session.user?.backup_codes?.remaining;
      if (result.backup_code_used && remaining !== undefined) {
        SingleToast.push(texts.code.backupCodeUsed(remaining));
      }
      applySession(result.session);
    } catch (error) {
      if (isApiError(error, 'login_locked')) {
        // Сервер удалил сессию шага: возврат на вход с объяснением.
        endSession({ kind: 'error', text: loginLockedText(error) });
      } else if (isApiError(error, 'login_step_expired')) {
        endSession({ kind: 'error', text: texts.login.codeStepExpired });
      } else if (isApiError(error, 'too_many_attempts')) {
        setNotice(tooManyAttemptsText(error));
      } else if (isApiError(error) && FIELD_ERRORS[error.code]) {
        setFieldError(FIELD_ERRORS[error.code] ?? null);
        setFocusTick((tick) => tick + 1);
      } else if (!isApiError(error, 'unauthenticated') && !isApiError(error, 'login_step_required')) {
        setNotice(errorText(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const onCodeChange = (next: string) => {
    setValue(next);
    // После шестой цифры форма отправляется сама; то же значение повторно — только кнопкой.
    if (shouldAutoSubmit(next)) {
      void send(next);
    }
  };

  const switchMode = (next: Mode) => {
    setMode(next);
    setValue('');
    setFieldError(null);
    setNotice(null);
    setFocusTick((tick) => tick + 1);
  };

  const otherLogin = () => {
    logout().catch((error: unknown) => setNotice(errorText(error)));
  };

  return (
    <AuthLayout title={title}>
      <p>{mode === 'app' ? texts.code.intro : texts.code.backupIntro}</p>
      <form
        className={styles.form}
        noValidate
        onSubmit={(event) => {
          event.preventDefault();
          void send(value);
        }}
      >
        {notice && <Notice kind="error">{notice}</Notice>}
        <Field label={title} hideLabel error={fieldError}>
          {(control) =>
            mode === 'app' ? (
              <CodeInput
                {...control}
                inputRef={inputRef}
                value={value}
                onValueChange={onCodeChange}
                disabled={submitting}
              />
            ) : (
              <div className={styles.mono}>
                <Input
                  {...control}
                  ref={inputRef}
                  size="large"
                  width="100%"
                  autoComplete="off"
                  value={value}
                  onValueChange={setValue}
                  disabled={submitting}
                />
              </div>
            )
          }
        </Field>
        <Button type="submit" use="primary" size="large" width="100%" loading={submitting}>
          {texts.code.submit}
        </Button>
      </form>
      <div className={styles.links}>
        {mode === 'app' ? (
          <span>
            {texts.code.noPhone}{' '}
            <Link component="button" onClick={() => switchMode('backup')}>
              {texts.code.useBackupCode}
            </Link>
          </span>
        ) : (
          <span>
            <Link component="button" onClick={() => switchMode('app')}>
              {texts.code.useAppCode}
            </Link>
          </span>
        )}
        <span>
          <Link component="button" onClick={otherLogin}>
            {texts.code.otherLogin}
          </Link>
        </span>
      </div>
    </AuthLayout>
  );
}
