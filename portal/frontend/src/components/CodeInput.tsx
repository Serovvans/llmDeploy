import { Input } from '@skbkontur/react-ui';
import { useCallback, useRef } from 'react';

import { texts } from '../texts';
import styles from './CodeInput.module.css';

interface CodeInputProps {
  id: string;
  value: string;
  onValueChange: (value: string) => void;
  error: boolean;
  disabled: boolean;
  'aria-describedby': string | undefined;
  inputRef: React.Ref<Input>;
}

export const CODE_LENGTH = 6;

const FULL_CODE = new RegExp(`^\\d{${CODE_LENGTH}}$`);

/** Код без пробелов: сервер их отбрасывает (контракт §2.4). */
export function normalizeCode(value: string): string {
  return value.replace(/\s/g, '');
}

/**
 * Проверка кода до отправки (концепция §5.2): неполный код не отправляется —
 * сервер счёл бы его неудачной попыткой. Возвращает текст ошибки или `null`.
 */
export function checkCode(value: string): string | null {
  const code = normalizeCode(value);
  if (!code) {
    return texts.code.enterCode;
  }
  return FULL_CODE.test(code) ? null : texts.code.sixDigits;
}

/**
 * Автоотправка после шестой цифры: один раз на каждое введённое шестизначное значение.
 * Возвращает проверку «это значение пора отправить само»; то же значение повторно — только кнопкой.
 */
export function useAutoSubmitOnce(): (value: string) => boolean {
  const sent = useRef(new Set<string>());
  return useCallback((value) => {
    const code = normalizeCode(value);
    if (!FULL_CODE.test(code) || sent.current.has(code)) {
      return false;
    }
    sent.current.add(code);
    return true;
  }, []);
}

/**
 * Поле шестизначного кода: одно поле, а не шесть клеток (концепция §5.2).
 * Предел длины держит обработчик, а не атрибут `maxLength`: атрибут обрезал бы вставленное
 * «123 456» до «123 45» — приложения-аутентификаторы часто показывают код с пробелом.
 */
export function CodeInput({ inputRef, onValueChange, ...props }: CodeInputProps) {
  return (
    <div className={styles.code}>
      <Input
        ref={inputRef}
        size="large"
        width="100%"
        inputMode="numeric"
        autoComplete="one-time-code"
        autoFocus
        onValueChange={(value) => onValueChange(normalizeCode(value).slice(0, CODE_LENGTH))}
        {...props}
      />
    </div>
  );
}
