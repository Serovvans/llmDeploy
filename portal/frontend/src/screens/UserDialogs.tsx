import { Button, Gapped, Input, Link, Modal, Radio, RadioGroup, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { AdminUser, Role, TemporaryPasswordResult } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { Field } from '../components/Field';
import { useCopy } from '../hooks/useCopy';
import { errorText, texts } from '../texts';
import styles from './UserDialogs.module.css';

const FULL_NAME_MAX = 200;
const MODAL_WIDTH = 480;
const form = texts.users.form;

function checkFullName(fullName: string): string | null {
  if (!fullName.trim()) {
    return form.enterFullName;
  }
  return fullName.trim().length > FULL_NAME_MAX ? form.fullNameTooLong : null;
}

const FULL_NAME_ERRORS: Record<string, string> = { required: form.enterFullName, too_long: form.fullNameTooLong };
const LOGIN_ERRORS: Record<string, string> = { required: form.enterLogin, invalid_format: form.loginInvalid };

interface FormErrors {
  fullName: string | null;
  login: string | null;
}

/** Ошибки полей из отказа сервера (концепция §5.10); `null` — отказ не про поля формы. */
function formErrorsFrom(error: unknown): FormErrors | null {
  if (isApiError(error, 'login_taken')) {
    return { fullName: null, login: form.loginTaken };
  }
  if (!isApiError(error, 'validation_error') || error.fields.length === 0) {
    return null;
  }
  const errors: FormErrors = { fullName: null, login: null };
  for (const { field, code, message } of error.fields) {
    if (field === 'full_name') {
      errors.fullName = FULL_NAME_ERRORS[code] ?? message;
    } else if (field === 'login') {
      errors.login = LOGIN_ERRORS[code] ?? message;
    }
  }
  return errors.fullName || errors.login ? errors : null;
}

function RoleField({ role, onChange, disabled }: { role: Role; onChange: (role: Role) => void; disabled: boolean }) {
  return (
    <fieldset className={styles.fieldset}>
      <legend className={styles.legend}>{form.roleLabel}</legend>
      <RadioGroup<Role> value={role} onValueChange={onChange} disabled={disabled}>
        <Gapped vertical gap={8}>
          <Radio<Role> value="employee">{form.roleEmployee}</Radio>
          <Radio<Role> value="admin">{form.roleAdmin}</Radio>
        </Gapped>
      </RadioGroup>
    </fieldset>
  );
}

interface TemporaryPasswordProps {
  title: string;
  result: TemporaryPasswordResult;
  onClose: () => void;
}

/** Временный пароль показывается один раз: окно закрывается только кнопкой или крестиком. */
function TemporaryPasswordContent({ title, result, onClose }: TemporaryPasswordProps) {
  const copy = useCopy();
  const labels = texts.users.temporaryPassword;
  return (
    <>
      <Modal.Header>{title}</Modal.Header>
      <Modal.Body>
        <div className={styles.stack}>
          <p>{labels.intro}</p>
          <dl className={styles.credentials}>
            <dt>{labels.loginLabel}</dt>
            <dd className="p-mono">{result.user.login}</dd>
            <dt>{labels.passwordLabel}</dt>
            <dd>
              <span className="p-mono">{result.temporary_password}</span>
              <Link component="button" onClick={() => void copy.copy(result.temporary_password)}>
                {copy.label}
              </Link>
            </dd>
          </dl>
          <p>{labels.outro}</p>
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Button use="primary" onClick={onClose}>
          {labels.done}
        </Button>
      </Modal.Footer>
    </>
  );
}

export function TemporaryPasswordModal(props: TemporaryPasswordProps) {
  return (
    <Modal width={MODAL_WIDTH} ignoreBackgroundClick onClose={props.onClose}>
      <TemporaryPasswordContent {...props} />
    </Modal>
  );
}

interface CreateUserModalProps {
  /** Вызывается после закрытия; `created` — учётная запись была создана, список надо обновить. */
  onClose: (created: boolean) => void;
}

export function CreateUserModal({ onClose }: CreateUserModalProps) {
  const [fullName, setFullName] = useState('');
  const [login, setLogin] = useState('');
  const [role, setRole] = useState<Role>('employee');
  const [errors, setErrors] = useState<FormErrors>({ fullName: null, login: null });
  const [submitting, setSubmitting] = useState(false);
  const [created, setCreated] = useState<TemporaryPasswordResult | null>(null);
  const fullNameRef = useRef<Input>(null);
  const loginRef = useRef<Input>(null);

  // Фокус — в первое поле с ошибкой.
  useEffect(() => {
    if (errors.fullName) {
      fullNameRef.current?.focus();
    } else if (errors.login) {
      loginRef.current?.focus();
    }
  }, [errors]);

  const submit = async (event: React.SyntheticEvent) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    const checked = { fullName: checkFullName(fullName), login: login.trim() ? null : form.enterLogin };
    setErrors(checked);
    if (checked.fullName || checked.login) {
      return;
    }
    setSubmitting(true);
    try {
      setCreated(await api.createUser({ full_name: fullName.trim(), login: login.trim(), role }));
    } catch (error) {
      const fieldErrors = formErrorsFrom(error);
      if (fieldErrors) {
        setErrors(fieldErrors);
      } else {
        SingleToast.push(errorText(error), { use: 'error' });
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (created) {
    return (
      <TemporaryPasswordModal
        title={texts.users.temporaryPassword.createdTitle}
        result={created}
        onClose={() => onClose(true)}
      />
    );
  }

  return (
    <Modal width={MODAL_WIDTH} ignoreBackgroundClick onClose={() => onClose(false)}>
      <Modal.Header>{form.createTitle}</Modal.Header>
      <Modal.Body>
        <form className={styles.stack} onSubmit={submit} noValidate>
          <Field label={form.fullNameLabel} error={errors.fullName}>
            {(control) => (
              <Input
                {...control}
                ref={fullNameRef}
                width="100%"
                autoFocus
                value={fullName}
                onValueChange={setFullName}
                disabled={submitting}
              />
            )}
          </Field>
          <Field label={form.loginLabel} error={errors.login} hint={form.loginHint}>
            {(control) => (
              <Input
                {...control}
                ref={loginRef}
                width="100%"
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                value={login}
                onValueChange={setLogin}
                disabled={submitting}
              />
            )}
          </Field>
          <RoleField role={role} onChange={setRole} disabled={submitting} />
          <button type="submit" hidden />
        </form>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={submitting} onClick={submit}>
            {form.create}
          </Button>
          <Button disabled={submitting} onClick={() => onClose(false)}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}

interface EditUserModalProps {
  user: AdminUser;
  /** `changed` — учётная запись изменена, список надо обновить. */
  onClose: (changed: boolean) => void;
}

export function EditUserModal({ user, onClose }: EditUserModalProps) {
  const [fullName, setFullName] = useState(user.full_name);
  const [role, setRole] = useState<Role>(user.role);
  // Объект, а не строка: повтор той же ошибки снова ставит фокус в поле.
  const [fullNameFailure, setFullNameError] = useState<{ text: string } | null>(null);
  const fullNameError = fullNameFailure?.text ?? null;
  const fullNameRef = useRef<Input>(null);

  useEffect(() => {
    if (fullNameFailure) {
      fullNameRef.current?.focus();
    }
  }, [fullNameFailure]);
  const [submitting, setSubmitting] = useState(false);
  const [confirmingRole, setConfirmingRole] = useState(false);

  const roleChanged = role !== user.role;
  const nameChanged = fullName.trim() !== user.full_name;

  const save = async () => {
    setConfirmingRole(false);
    setSubmitting(true);
    try {
      // Отправляются только изменённые поля (контракт §3).
      await api.updateUser(user.id, {
        ...(nameChanged ? { full_name: fullName.trim() } : {}),
        ...(roleChanged ? { role } : {}),
      });
      SingleToast.push(roleChanged ? texts.users.toast.roleChanged : texts.users.toast.updated);
      onClose(true);
    } catch (error) {
      const fieldErrors = formErrorsFrom(error);
      if (fieldErrors?.fullName) {
        setFullNameError({ text: fieldErrors.fullName });
      } else {
        showUserActionError(error);
        onClose(true);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const submit = (event: React.SyntheticEvent) => {
    event.preventDefault();
    if (submitting) {
      return;
    }
    if (!nameChanged && !roleChanged) {
      onClose(false);
      return;
    }
    const error = checkFullName(fullName);
    setFullNameError(error ? { text: error } : null);
    if (error) {
      return;
    }
    if (roleChanged) {
      setConfirmingRole(true);
    } else {
      void save();
    }
  };

  return (
    <>
      <Modal width={MODAL_WIDTH} ignoreBackgroundClick onClose={() => onClose(false)}>
        <Modal.Header>{form.editTitle}</Modal.Header>
        <Modal.Body>
          <form className={styles.stack} onSubmit={submit} noValidate>
            <div>
              <div className={styles.label}>{form.loginLabel}</div>
              <div className="p-mono">{user.login}</div>
              <div className={styles.hint}>{form.loginReadonly}</div>
            </div>
            <Field label={form.fullNameLabel} error={fullNameError}>
              {(control) => (
                <Input
                  {...control}
                  ref={fullNameRef}
                  width="100%"
                  autoFocus
                  value={fullName}
                  onValueChange={setFullName}
                  disabled={submitting}
                />
              )}
            </Field>
            <RoleField role={role} onChange={setRole} disabled={submitting} />
            <button type="submit" hidden />
          </form>
        </Modal.Body>
        <Modal.Footer>
          <Gapped gap={8}>
            <Button use="primary" loading={submitting} onClick={submit}>
              {form.save}
            </Button>
            <Button disabled={submitting} onClick={() => onClose(false)}>
              {texts.common.cancel}
            </Button>
          </Gapped>
        </Modal.Footer>
      </Modal>
      {confirmingRole && (
        <ConfirmModal
          {...texts.users.confirm.changeRole(role)}
          who={texts.users.confirm.who(user.full_name, user.login)}
          onConfirm={() => void save()}
          onCancel={() => setConfirmingRole(false)}
        />
      )}
    </>
  );
}

/** Отказ действия над пользователем: свой текст для `cannot_modify_self`, иначе общий (концепция §5.10). */
export function showUserActionError(error: unknown): void {
  if (isApiError(error, 'unauthenticated')) {
    // Сеанс завершён: интерфейс уже переходит на вход.
    return;
  }
  const text = isApiError(error, 'cannot_modify_self')
    ? texts.users.toast.cannotModifySelf
    : isApiError(error)
      ? texts.common.actionFailed
      : errorText(error);
  SingleToast.push(text, { use: 'error' });
}
