import { Button, Input, Link, Loader } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { SecondFactorConfirmResult, SecondFactorSetup } from '../api/types';
import { AuthLayout } from '../components/AuthLayout';
import { checkCode, CodeInput, normalizeCode, useAutoSubmitOnce } from '../components/CodeInput';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { QrCode } from '../components/QrCode';
import { StepList } from '../components/StepList';
import { useCopy } from '../hooks/useCopy';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import { BackupCodes } from './BackupCodes';
import styles from './SecondFactorScreen.module.css';

type SetupState = { status: 'loading' } | { status: 'failed' } | { status: 'ready'; setup: SecondFactorSetup };

function groupsOfFour(secret: string): string {
  return secret.match(/.{1,4}/g)?.join(' ') ?? secret;
}

/** Настройка второго фактора и резервные коды (концепция §5.4). */
export function SecondFactorScreen() {
  const { applySession, endSession } = useSession();
  const [setupState, setSetupState] = useState<SetupState>({ status: 'loading' });
  const [setupAttempt, setSetupAttempt] = useState(0);
  const [confirmed, setConfirmed] = useState<SecondFactorConfirmResult | null>(null);
  const [manualOpen, setManualOpen] = useState(false);
  const [code, setCode] = useState('');
  const [codeError, setCodeError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [focusTick, setFocusTick] = useState(0);
  const codeRef = useRef<Input>(null);
  const secretCopy = useCopy();
  usePageTitle(confirmed ? texts.backupCodes.title : texts.secondFactor.title);

  const stepExpired = useCallback(
    () => endSession({ kind: 'error', text: texts.login.loginStepExpired }),
    [endSession],
  );

  useEffect(() => {
    let cancelled = false;
    api.startSecondFactorSetup().then(
      (setup) => {
        if (!cancelled) {
          setSetupState({ status: 'ready', setup });
        }
      },
      (error: unknown) => {
        if (cancelled) {
          return;
        }
        if (isApiError(error, 'login_step_expired')) {
          stepExpired();
        } else {
          setSetupState({ status: 'failed' });
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [setupAttempt, stepExpired]);

  const reloadSetup = () => {
    setSetupState({ status: 'loading' });
    setSetupAttempt((attempt) => attempt + 1);
  };

  const shouldAutoSubmit = useAutoSubmitOnce();

  // После ошибки содержимое поля выделяется: новый код набирается поверх.
  useEffect(() => {
    if (focusTick > 0) {
      codeRef.current?.focus();
      codeRef.current?.selectAll();
    }
  }, [focusTick]);

  const confirm = async (entered: string) => {
    if (submitting) {
      return;
    }
    const invalid = checkCode(entered);
    setCodeError(invalid);
    if (invalid) {
      setFocusTick((tick) => tick + 1);
      return;
    }
    setNotice(null);
    setSubmitting(true);
    try {
      setConfirmed(await api.confirmSecondFactor(normalizeCode(entered)));
    } catch (error) {
      if (isApiError(error, 'invalid_code')) {
        setCodeError(texts.secondFactor.invalidCode);
        setFocusTick((tick) => tick + 1);
      } else if (isApiError(error, 'setup_not_started')) {
        // Без сообщения: настройка запрашивается заново, поле кода очищается.
        setCode('');
        reloadSetup();
      } else if (isApiError(error, 'login_step_expired')) {
        stepExpired();
      } else if (!isApiError(error, 'unauthenticated') && !isApiError(error, 'login_step_required')) {
        setNotice(errorText(error));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const onCodeChange = (next: string) => {
    setCode(next);
    if (shouldAutoSubmit(next)) {
      void confirm(next);
    }
  };

  if (confirmed) {
    return (
      <AuthLayout title={texts.backupCodes.title} wide>
        <BackupCodes codes={confirmed.backup_codes} onProceed={() => applySession(confirmed.session)} />
      </AuthLayout>
    );
  }

  return (
    <AuthLayout title={texts.secondFactor.title} wide>
      <p>{texts.secondFactor.intro}</p>
      <StepList
        steps={[
          { title: texts.secondFactor.step1Title, children: <p>{texts.secondFactor.step1Text}</p> },
          {
            title: texts.secondFactor.step2Title,
            children: (
              <div className={styles.scan}>
                <div className={styles.scanText}>
                  <p>{texts.secondFactor.step2Text}</p>
                  {setupState.status === 'ready' && (
                    <>
                      <span>
                        <Link
                          component="button"
                          aria-expanded={manualOpen}
                          onClick={() => setManualOpen((open) => !open)}
                        >
                          {texts.secondFactor.cannotScan}
                        </Link>
                      </span>
                      {manualOpen && (
                        <div className={styles.manual}>
                          <p>{texts.secondFactor.manualEntry}</p>
                          <p className={styles.secret}>
                            <span className="p-mono">{groupsOfFour(setupState.setup.secret)}</span>
                            <Link component="button" onClick={() => void secretCopy.copy(setupState.setup.secret)}>
                              {secretCopy.label}
                            </Link>
                          </p>
                        </div>
                      )}
                    </>
                  )}
                </div>
                <div className={styles.qr}>
                  {setupState.status === 'ready' && (
                    <QrCode
                      size={setupState.setup.qr.size}
                      path={setupState.setup.qr.path}
                      label={texts.secondFactor.qrLabel}
                    />
                  )}
                  {setupState.status === 'loading' && (
                    <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
                      <div className={styles.qrPlaceholder} />
                    </Loader>
                  )}
                  {setupState.status === 'failed' && (
                    <Notice kind="error" action={{ label: texts.common.retry, onClick: reloadSetup }}>
                      {texts.secondFactor.qrFailed}
                    </Notice>
                  )}
                </div>
              </div>
            ),
          },
          {
            title: texts.secondFactor.step3Title,
            children: (
              <>
                <p>{texts.secondFactor.step3Text}</p>
                {notice && <Notice kind="error">{notice}</Notice>}
                <form
                  className={styles.confirm}
                  noValidate
                  onSubmit={(event) => {
                    event.preventDefault();
                    void confirm(code);
                  }}
                >
                  <div className={styles.codeField}>
                    <Field label={texts.secondFactor.step3Title} hideLabel error={codeError}>
                      {(control) => (
                        <div className={styles.codeBox}>
                          <CodeInput
                            {...control}
                            inputRef={codeRef}
                            value={code}
                            onValueChange={onCodeChange}
                            disabled={submitting}
                          />
                        </div>
                      )}
                    </Field>
                  </div>
                  <Button type="submit" use="primary" size="large" loading={submitting}>
                    {texts.secondFactor.submit}
                  </Button>
                </form>
              </>
            ),
          },
        ]}
      />
    </AuthLayout>
  );
}
