import { Button, Gapped, Modal, PasswordInput, ScrollContainer, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { SessionUser } from '../api/types';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { PageHeader } from '../components/PageHeader';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import { checkNewPassword, loginLockedText, NO_PASSWORD_ERRORS, passwordErrorsFrom } from './authErrors';
import { NewPasswordFields } from './NewPasswordFields';
import styles from './ProfileScreen.module.css';

const LOW_BACKUP_CODES = 2;
const t = texts.profile;

export function ProfileScreen({ user }: { user: SessionUser }) {
  usePageTitle(t.title);
  const { logout } = useSession();
  const [changingPassword, setChangingPassword] = useState(false);
  const rememberOpener = useFocusReturn(changingPassword);

  const onLogout = () => {
    logout().catch((error: unknown) => SingleToast.push(errorText(error), { use: 'error' }));
  };

  return (
    <>
      <PageHeader title={t.title} />
      <ScrollContainer>
        <div className={styles.column}>
          <section className={styles.block}>
            <h2 className={styles.heading}>{t.accountTitle}</h2>
            <dl className={styles.rows}>
              <dt>{t.fullName}</dt>
              <dd>{user.full_name}</dd>
              <dt>{t.login}</dt>
              <dd className="p-mono">{user.login}</dd>
              <dt>{t.role}</dt>
              <dd>{texts.users.role[user.role]}</dd>
            </dl>
            <p className={styles.note}>{t.accountNote}</p>
          </section>
          <section className={styles.block}>
            <h2 className={styles.heading}>{t.loginTitle}</h2>
            <dl className={styles.rows}>
              <dt>{t.password}</dt>
              <dd data-opener="password">
                <Button
                  onClick={() => {
                    rememberOpener('[data-opener="password"] button');
                    setChangingPassword(true);
                  }}
                >
                  {t.changePassword}
                </Button>
              </dd>
              <dt>{t.appCode}</dt>
              <dd>{t.appCodeConfigured}</dd>
              {user.backup_codes && (
                <>
                  <dt>{t.backupCodes}</dt>
                  <dd>
                    {t.backupCodesLeft(user.backup_codes.remaining, user.backup_codes.total)}
                    {user.backup_codes.remaining <= LOW_BACKUP_CODES && (
                      <p className={styles.note}>{t.backupCodesLow}</p>
                    )}
                  </dd>
                </>
              )}
            </dl>
            <p className={styles.note}>{t.loginNote}</p>
          </section>
          <div>
            <Button onClick={onLogout}>{t.logout}</Button>
          </div>
        </div>
      </ScrollContainer>
      {changingPassword && <ChangePasswordModal onClose={() => setChangingPassword(false)} />}
    </>
  );
}

/** Добровольная смена пароля: сервер гасит все сессии, сотрудник входит заново (концепция §5.11). */
function ChangePasswordModal({ onClose }: { onClose: () => void }) {
  const { config, endSession } = useSession();
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [repeat, setRepeat] = useState('');
  const [errors, setErrors] = useState(NO_PASSWORD_ERRORS);
  const [notice, setNotice] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const currentRef = useRef<PasswordInput>(null);
  const nextRef = useRef<PasswordInput>(null);
  const repeatRef = useRef<PasswordInput>(null);
  const limits = config?.password ?? null;

  useEffect(() => {
    if (errors.current) {
      currentRef.current?.focus();
    } else if (errors.next) {
      nextRef.current?.focus();
    } else if (errors.repeat) {
      repeatRef.current?.focus();
    }
  }, [errors]);

  const submit = async (event: React.SyntheticEvent) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    const checked = {
      ...checkNewPassword(next, repeat, limits),
      current: current ? null : texts.password.enterCurrent,
    };
    setErrors(checked);
    setNotice(null);
    if (checked.current || checked.next || checked.repeat) {
      return;
    }
    setSubmitting(true);
    try {
      await api.changeOwnPassword(current, next);
      endSession({ kind: 'info', text: texts.login.passwordChanged }, true);
    } catch (error) {
      const fieldErrors = passwordErrorsFrom(error, limits);
      if (fieldErrors) {
        setErrors(fieldErrors);
      } else if (isApiError(error, 'login_locked')) {
        // Неверный текущий пароль — неудачная попытка входа: окно остаётся открытым, поля не очищаются.
        setNotice(loginLockedText(error));
      } else if (!isApiError(error, 'unauthenticated')) {
        SingleToast.push(errorText(error), { use: 'error' });
      }
      setSubmitting(false);
    }
  };

  return (
    <Modal width={480} ignoreBackgroundClick onClose={onClose}>
      <Modal.Header>{t.changePassword}</Modal.Header>
      <Modal.Body>
        <form className={styles.form} onSubmit={submit} noValidate>
          {notice && <Notice kind="error">{notice}</Notice>}
          <Field label={texts.password.currentLabel} error={errors.current}>
            {(control) => (
              <PasswordInput
                {...control}
                ref={currentRef}
                width="100%"
                autoComplete="current-password"
                autoFocus
                value={current}
                onValueChange={setCurrent}
                disabled={submitting}
              />
            )}
          </Field>
          <NewPasswordFields
            next={next}
            repeat={repeat}
            onNextChange={setNext}
            onRepeatChange={setRepeat}
            nextError={errors.next}
            repeatError={errors.repeat}
            minLength={limits?.min_length ?? null}
            disabled={submitting}
            size="small"
            nextRef={nextRef}
            repeatRef={repeatRef}
          />
          <button type="submit" hidden />
        </form>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={submitting} onClick={submit}>
            {texts.password.submit}
          </Button>
          <Button disabled={submitting} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
