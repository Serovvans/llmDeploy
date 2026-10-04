import { PasswordInput } from '@skbkontur/react-ui';

import { Field } from '../components/Field';
import { texts } from '../texts';

interface NewPasswordFieldsProps {
  next: string;
  repeat: string;
  onNextChange: (value: string) => void;
  onRepeatChange: (value: string) => void;
  nextError: string | null;
  repeatError: string | null;
  /** Минимальная длина из конфигурации сервера; `null`, пока она не получена. */
  minLength: number | null;
  disabled: boolean;
  size: 'small' | 'large';
  autoFocus?: boolean;
  nextRef: React.Ref<PasswordInput>;
  repeatRef: React.Ref<PasswordInput>;
}

/** Поля «Новый пароль» и «Повторите пароль» — общие для обязательной и добровольной смены (концепция §5.3). */
export function NewPasswordFields(props: NewPasswordFieldsProps) {
  return (
    <>
      <Field
        label={texts.password.newLabel}
        error={props.nextError}
        hint={props.minLength === null ? undefined : texts.password.hint(props.minLength)}
      >
        {(control) => (
          <PasswordInput
            {...control}
            ref={props.nextRef}
            size={props.size}
            width="100%"
            autoComplete="new-password"
            autoFocus={props.autoFocus}
            value={props.next}
            onValueChange={props.onNextChange}
            disabled={props.disabled}
          />
        )}
      </Field>
      <Field label={texts.password.repeatLabel} error={props.repeatError}>
        {(control) => (
          <PasswordInput
            {...control}
            ref={props.repeatRef}
            size={props.size}
            width="100%"
            autoComplete="new-password"
            value={props.repeat}
            onValueChange={props.onRepeatChange}
            disabled={props.disabled}
          />
        )}
      </Field>
    </>
  );
}
