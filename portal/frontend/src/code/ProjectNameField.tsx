import { Input } from '@skbkontur/react-ui';

import { Field } from '../components/Field';
import { texts } from '../texts';
import { projectNameError } from './rules';

const w = texts.workshop;

interface ProjectNameFieldProps {
  value: string;
  error: string | null;
  disabled?: boolean;
  inputRef?: React.Ref<Input>;
  onChange: (value: string) => void;
}

/**
 * «Имя проекта» — одни правила и тексты в мастере, окнах примера, ZIP и переноса (§5.13.4): под полем,
 * пока имя введено и верно, — файл DLL и класс для CoGIS.
 */
export function ProjectNameField({ value, error, disabled, inputRef, onChange }: ProjectNameFieldProps) {
  return (
    <Field
      label={w.name.label}
      error={error}
      hint={
        <>
          {w.name.hint}
          {value && !projectNameError(value) && ` ${w.name.derived(value)}`}
        </>
      }
    >
      {(control) => (
        <Input
          {...control}
          ref={inputRef}
          width={400}
          className="p-mono"
          spellCheck={false}
          autoComplete="off"
          value={value}
          disabled={disabled}
          onValueChange={onChange}
        />
      )}
    </Field>
  );
}
