import {
  Button,
  Gapped,
  Input,
  Link,
  PasswordInput,
  Radio,
  RadioGroup,
  SidePage,
  SingleToast,
  Spinner,
  Textarea,
  Toggle,
  Token,
  TokenInput,
  TokenInputType,
} from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { AdminDbConnection, DbConnectionInput, DbSnapshot, DbTlsMode, PortalConfig, SampleExclusion } from '../api/types';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { texts } from '../texts';
import styles from './ConnectionForm.module.css';
import { dbRefusal } from './refusal';
import { RightsCheck } from './RightsCheck';

const f = texts.db.form;
const e = f.errors;
const TITLE_MAX = 100;
const DESCRIPTION_MAX = 500;
const NAME_MAX_BYTES = 63;
const TLS_MODES: DbTlsMode[] = ['verify', 'require', 'disable'];
const USERS_SUGGESTED = 10;

type Person = { id: string; login: string; full_name: string };

interface Values {
  title: string;
  description: string;
  host: string;
  port: string;
  database: string;
  username: string;
  password: string;
  tls_mode: DbTlsMode;
  schemas: string[];
  samples_allowed: boolean;
  exclusions: string[];
  accessAll: boolean;
  users: Person[];
}

type Errors = Partial<Record<'title' | 'description' | 'host' | 'port' | 'database' | 'username' | 'password' | 'schemas' | 'exclusions' | 'access', string>>;

const bytes = (value: string) => new TextEncoder().encode(value).length;
// Управляющие символы — признак недопустимого имени (контракт §7.6.2).
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

export function exclusionText(item: SampleExclusion): string {
  return [item.schema, item.table, item.column].filter((part) => part !== null).join('.');
}

/** «схема.таблица» или «схема.таблица.столбец»; иначе `null`. */
export function parseExclusion(text: string): SampleExclusion | null {
  const parts = text.trim().split('.');
  if ((parts.length !== 2 && parts.length !== 3) || parts.some((part) => part === '')) {
    return null;
  }
  return { schema: parts[0] ?? '', table: parts[1] ?? '', column: parts[2] ?? null };
}

/** Проверка формы до отправки — те же правила, что у сервера (контракт §7.6.2). */
export function validateConnection(values: Values, isNew: boolean): Errors {
  const errors: Errors = {};
  const title = values.title.trim();
  if (!title) {
    errors.title = e.title;
  } else if (title.length > TITLE_MAX) {
    errors.title = e.titleLong;
  }
  if (values.description.length > DESCRIPTION_MAX) {
    errors.description = e.descriptionLong;
  }
  if (!values.host.trim()) {
    errors.host = e.host;
  }
  const port = Number(values.port);
  if (!/^\d+$/.test(values.port.trim()) || port < 1 || port > 65535) {
    errors.port = e.port;
  }
  if (!values.database.trim()) {
    errors.database = e.database;
  }
  if (!values.username.trim()) {
    errors.username = e.username;
  }
  if (isNew && !values.password) {
    errors.password = e.password;
  }
  const invalidSchema = values.schemas.find((name) => !name || bytes(name) > NAME_MAX_BYTES || CONTROL.test(name));
  if (values.schemas.length === 0) {
    errors.schemas = e.schemas;
  } else if (invalidSchema !== undefined) {
    errors.schemas = e.schemaInvalid(invalidSchema);
  }
  if (values.samples_allowed) {
    for (const text of values.exclusions) {
      const parsed = parseExclusion(text);
      if (!parsed) {
        errors.exclusions = e.exclusionFormat;
        break;
      }
      if (!values.schemas.includes(parsed.schema)) {
        errors.exclusions = e.exclusionSchema(parsed.schema);
        break;
      }
    }
  }
  if (!values.accessAll && values.users.length === 0) {
    errors.access = e.access;
  }
  return errors;
}

function initialValues(connection: AdminDbConnection | null, tlsDefault: DbTlsMode): Values {
  return {
    title: connection?.title ?? '',
    description: connection?.description ?? '',
    host: connection?.host ?? '',
    port: String(connection?.port ?? 5432),
    database: connection?.database ?? '',
    username: connection?.username ?? '',
    password: '',
    tls_mode: connection?.tls_mode ?? tlsDefault,
    schemas: connection?.schemas ?? [],
    samples_allowed: connection?.samples_allowed ?? false,
    exclusions: (connection?.sample_exclusions ?? []).map(exclusionText),
    accessAll: connection?.access.all ?? true,
    users: connection?.access.users ?? [],
  };
}

function toInput(values: Values): DbConnectionInput {
  return {
    title: values.title.trim(),
    description: values.description,
    host: values.host.trim(),
    port: Number(values.port),
    database: values.database.trim(),
    username: values.username.trim(),
    ...(values.password ? { password: values.password } : {}),
    tls_mode: values.tls_mode,
    schemas: values.schemas,
    samples_allowed: values.samples_allowed,
    sample_exclusions: values.exclusions.flatMap((text) => {
      const parsed = parseExclusion(text);
      return parsed ? [parsed] : [];
    }),
    access: values.accessAll ? { all: true } : { all: false, user_ids: values.users.map((user) => user.id) },
  };
}

/** Только изменённые поля — смена параметров входа сбрасывает итог проверки (контракт §7.6.2). */
function changedFields(next: DbConnectionInput, saved: AdminDbConnection): Partial<DbConnectionInput> {
  const before = toInput(initialValues(saved, saved.tls_mode));
  return Object.fromEntries(
    Object.entries(next).filter(([key, value]) => JSON.stringify(value) !== JSON.stringify(before[key as keyof DbConnectionInput])),
  );
}

/** Ошибки сервера по полям (контракт §7.6.2) — на тексты под полями. */
function serverErrors(error: unknown, values: Values): Errors | null {
  if (isApiError(error, 'db_connection_title_taken')) {
    return { title: e.titleTaken };
  }
  if (!isApiError(error, 'validation_error')) {
    return null;
  }
  const errors: Errors = {};
  for (const { field, code } of error.fields) {
    if (field === 'title') {
      errors.title = code === 'too_long' ? e.titleLong : e.title;
    } else if (field === 'description') {
      errors.description = e.descriptionLong;
    } else if (field === 'host') {
      errors.host = e.host;
    } else if (field === 'port') {
      errors.port = e.port;
    } else if (field === 'database') {
      errors.database = e.database;
    } else if (field === 'username') {
      errors.username = e.username;
    } else if (field === 'password') {
      errors.password = e.password;
    } else if (field.startsWith('schemas')) {
      errors.schemas = code === 'required' ? e.schemas : e.schemaInvalid(values.schemas.join(', '));
    } else if (field.startsWith('sample_exclusions')) {
      errors.exclusions = code === 'unknown_value' ? e.exclusionSchema(values.exclusions.map((text) => text.split('.')[0]).join(', ')) : e.exclusionFormat;
    } else if (field.startsWith('access')) {
      errors.access = e.access;
    }
  }
  return Object.keys(errors).length > 0 ? errors : null;
}

const ORDER: (keyof Errors)[] = ['title', 'description', 'host', 'port', 'database', 'username', 'password', 'schemas', 'exclusions', 'access'];

interface Props {
  connection: AdminDbConnection | null;
  config: PortalConfig;
  /** Открыть сразу на блоке «Проверка» (ссылка из столбца «Проверка прав»). */
  focusCheck?: boolean;
  onChanged: () => void;
  onFeatureOff: () => void;
  onClose: () => void;
}

/** Форма подключения — `SidePage` 640 px, блокирующий (концепция §5.14). */
export function ConnectionForm({ connection, config, focusCheck = false, onChanged, onFeatureOff, onClose }: Props) {
  const [saved, setSaved] = useState(connection);
  const [values, setValues] = useState(() =>
    initialValues(connection, config.db.tls_verify_available ? 'verify' : 'require'),
  );
  const [errors, setErrors] = useState<Errors>({});
  const [failure, setFailure] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [newPassword, setNewPassword] = useState(false);
  const [loadingSnapshot, setLoadingSnapshot] = useState(false);
  const [snapshot, setSnapshot] = useState<DbSnapshot | null>(null);
  const form = useRef<HTMLFormElement>(null);
  const check = useRef<HTMLHeadingElement>(null);
  const result = useRef<HTMLDivElement>(null);
  const [checked, setChecked] = useState(0);

  useEffect(() => {
    if (focusCheck) {
      check.current?.scrollIntoView({ block: 'start' });
      check.current?.focus();
    }
  }, [focusCheck]);

  // После «Сохранить и проверить» — фокус на итог проверки (§9).
  useEffect(() => {
    if (checked > 0) {
      result.current?.focus();
    }
  }, [checked]);

  // Подсказки «Не копировать никогда» — из загруженной схемы базы.
  const savedId = saved?.snapshot ? saved.id : null;
  useEffect(() => {
    if (!savedId) {
      return;
    }
    api.getDbSnapshot(savedId).then(setSnapshot, () => {
      // Без схемы поле принимает свободный ввод.
    });
  }, [savedId]);

  // Ошибки — в первое поле с ошибкой.
  useEffect(() => {
    const first = ORDER.find((key) => errors[key]);
    if (first) {
      form.current?.querySelector<HTMLElement>(`[data-field="${first}"] :is(input, textarea)`)?.focus();
    }
  }, [errors]);

  const set = <K extends keyof Values>(key: K, value: Values[K]) => setValues((current) => ({ ...current, [key]: value }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const found = validateConnection({ ...values, password: saved && !newPassword ? 'saved' : values.password }, saved === null);
    setErrors(found);
    setFailure(null);
    if (Object.keys(found).length > 0) {
      return;
    }
    setPending(true);
    try {
      const input = toInput(values);
      let next: AdminDbConnection;
      if (saved) {
        const changes = changedFields(input, saved);
        next = Object.keys(changes).length > 0 ? await api.adminUpdateDbConnection(saved.id, changes) : saved;
      } else {
        next = await api.adminCreateDbConnection(input);
      }
      setSaved(next);
      setValues((current) => ({ ...current, password: '' }));
      setNewPassword(false);
      onChanged();
      SingleToast.push(f.saved);
      setSaved(await api.adminCheckDbConnection(next.id));
      setChecked((n) => n + 1);
      onChanged();
    } catch (error) {
      const fields = serverErrors(error, values);
      if (fields) {
        setErrors(fields);
      } else if (isApiError(error, 'feature_disabled')) {
        onFeatureOff();
      } else if (isApiError(error, 'not_found')) {
        SingleToast.push(texts.db.admin.alreadyRemoved);
        onChanged();
        onClose();
      } else if (!isApiError(error, 'unauthenticated')) {
        setFailure(texts.common.actionFailed);
      }
    } finally {
      setPending(false);
    }
  };

  const loadSnapshot = async () => {
    if (!saved) {
      return;
    }
    setLoadingSnapshot(true);
    try {
      const loaded = await api.refreshDbSnapshot(saved.id);
      SingleToast.push(texts.db.picker.refreshed(loaded.tables.length));
      setSnapshot(loaded);
      setSaved({ ...saved, snapshot: { taken_at: loaded.taken_at, tables: loaded.tables.length, truncated: loaded.truncated } });
      onChanged();
    } catch (error) {
      if (isApiError(error, 'feature_disabled')) {
        onFeatureOff();
      } else if (!isApiError(error, 'unauthenticated')) {
        SingleToast.push(dbRefusal(error) ?? texts.common.actionFailed, { use: 'error' });
      }
    } finally {
      setLoadingSnapshot(false);
    }
  };

  const suggestions = async (query: string): Promise<string[]> => {
    const needle = query.trim().toLowerCase();
    if (!snapshot || !needle) {
      return [];
    }
    const names = snapshot.tables.flatMap((table) => [
      `${table.schema}.${table.name}`,
      ...table.columns.map((column) => `${table.schema}.${table.name}.${column.name}`),
    ]);
    return names.filter((name) => name.toLowerCase().includes(needle)).slice(0, 20);
  };

  const findUsers = async (query: string): Promise<Person[]> => {
    const page = await api.listUsers({ page: 1, q: query.trim(), order: 'asc' });
    return page.items
      .filter((user) => !values.users.some((item) => item.id === user.id))
      .slice(0, USERS_SUGGESTED)
      .map((user) => ({ id: user.id, login: user.login, full_name: user.full_name }));
  };

  const person = (user: Person) => `${user.full_name} (${user.login})`;
  const tlsVerifyOff = !config.db.tls_verify_available;

  return (
    // Opened at «Проверка» — without the slide-in: the sticky header measures its position on scroll, and mid-slide it
    // would stay offset.
    <SidePage width={640} blockBackground ignoreOutsideClick disableClose={pending} disableAnimations={focusCheck} onClose={onClose}>
      <SidePage.Header>{saved ? f.editTitle(saved.title) : f.newTitle}</SidePage.Header>
      <SidePage.Body>
        <SidePage.Container>
          <form ref={form} id="db-connection-form" className={styles.form} onSubmit={(event) => void submit(event)} noValidate>
            {failure && <Notice kind="error">{failure}</Notice>}
            <fieldset className={styles.group}>
              <legend className={styles.legend}>{f.forEmployees}</legend>
              <div data-field="title">
                <Field label={f.title} error={errors.title}>
                  {(control) => (
                    <Input {...control} width="100%" value={values.title} disabled={pending} onValueChange={(value) => set('title', value)} />
                  )}
                </Field>
              </div>
              <div data-field="description">
                <Field label={f.description} error={errors.description} hint={f.descriptionHint}>
                  {(control) => (
                    <Textarea
                      {...control}
                      width="100%"
                      autoResize
                      rows={2}
                      maxRows={4}
                      value={values.description}
                      disabled={pending}
                      onValueChange={(value) => set('description', value)}
                    />
                  )}
                </Field>
              </div>
            </fieldset>

            <fieldset className={styles.group}>
              <legend className={styles.legend}>{f.server}</legend>
              <div className={styles.hostRow}>
                <div data-field="host" className={styles.host}>
                  <Field label={f.host} error={errors.host}>
                    {(control) => (
                      <Input
                        {...control}
                        width="100%"
                        className="p-mono"
                        placeholder={f.hostPlaceholder}
                        value={values.host}
                        disabled={pending}
                        onValueChange={(value) => set('host', value)}
                      />
                    )}
                  </Field>
                </div>
                <div data-field="port">
                  <Field label={f.port} error={errors.port}>
                    {(control) => (
                      <Input
                        {...control}
                        width={96}
                        className="p-mono"
                        inputMode="numeric"
                        value={values.port}
                        disabled={pending}
                        onValueChange={(value) => set('port', value)}
                      />
                    )}
                  </Field>
                </div>
              </div>
              <div data-field="database">
                <Field label={f.database} error={errors.database}>
                  {(control) => (
                    <Input {...control} width="100%" className="p-mono" value={values.database} disabled={pending} onValueChange={(value) => set('database', value)} />
                  )}
                </Field>
              </div>
              <div data-field="username">
                <Field label={f.username} error={errors.username}>
                  {(control) => (
                    <Input {...control} width="100%" className="p-mono" value={values.username} disabled={pending} onValueChange={(value) => set('username', value)} />
                  )}
                </Field>
              </div>
              <div data-field="password">
                {saved && !newPassword ? (
                  <div className={styles.passwordSaved}>
                    <span className={styles.label}>{f.password}</span>
                    <span>{f.passwordSaved}</span>
                    <Link component="button" onClick={() => setNewPassword(true)}>
                      {f.newPassword}
                    </Link>
                  </div>
                ) : (
                  <Field label={f.password} error={errors.password} hint={f.passwordHint}>
                    {(control) => (
                      <PasswordInput
                        {...control}
                        width="100%"
                        autoFocus={newPassword}
                        autoComplete="new-password"
                        value={values.password}
                        disabled={pending}
                        onValueChange={(value) => set('password', value)}
                      />
                    )}
                  </Field>
                )}
              </div>
              <fieldset className={styles.radios}>
                <legend className={styles.label}>{f.tls}</legend>
                <RadioGroup<DbTlsMode> value={values.tls_mode} disabled={pending} onValueChange={(value) => set('tls_mode', value)}>
                  <Gapped vertical gap={8}>
                    {TLS_MODES.map((mode) => (
                      <Radio<DbTlsMode> key={mode} value={mode} disabled={mode === 'verify' && tlsVerifyOff}>
                        <span className={styles.radioCaption}>
                          {f.tlsModes[mode]}
                          <span className={styles.note}>{mode === 'verify' && tlsVerifyOff ? f.noRootCert : f.tlsNotes[mode]}</span>
                        </span>
                      </Radio>
                    ))}
                  </Gapped>
                </RadioGroup>
              </fieldset>
              {values.tls_mode === 'disable' && <Notice kind="warning">{f.noTlsWarning}</Notice>}
              <div data-field="schemas">
                <Field label={f.schemas} error={errors.schemas} hint={f.schemasHint}>
                  {(control) => (
                    <TokenInput<string>
                      id={control.id}
                      aria-describedby={control['aria-describedby']}
                      error={control.error}
                      width="100%"
                      type={TokenInputType.WithoutReference}
                      selectedItems={values.schemas}
                      disabled={pending}
                      onValueChange={(items) => set('schemas', [...new Set(items.map((item) => item.trim()).filter(Boolean))])}
                    />
                  )}
                </Field>
              </div>
            </fieldset>

            <fieldset className={styles.group}>
              <legend className={styles.legend}>{f.samples}</legend>
              <div>
                <Toggle checked={values.samples_allowed} disabled={pending} onValueChange={(value) => set('samples_allowed', value)}>
                  {f.samplesToggle}
                </Toggle>
                <p className={styles.note}>{f.samplesHint(config.db.sample_rows_check)}</p>
              </div>
              {/* Исключения сохраняются и при выключенных образцах (контракт §7.6.2). */}
              {values.samples_allowed && (
                <div data-field="exclusions">
                  <Field label={f.exclusions} error={errors.exclusions} hint={f.exclusionsHint}>
                    {(control) => (
                      <TokenInput<string>
                        id={control.id}
                        aria-describedby={control['aria-describedby']}
                        error={control.error}
                        width="100%"
                        type={snapshot ? TokenInputType.Combined : TokenInputType.WithoutReference}
                        getItems={suggestions}
                        selectedItems={values.exclusions}
                        disabled={pending}
                        onValueChange={(items) => set('exclusions', [...new Set(items.map((item) => item.trim()).filter(Boolean))])}
                      />
                    )}
                  </Field>
                </div>
              )}
            </fieldset>

            <fieldset className={styles.group} data-field="access">
              <legend className={styles.legend}>{f.access}</legend>
              <RadioGroup<'all' | 'some'>
                value={values.accessAll ? 'all' : 'some'}
                disabled={pending}
                onValueChange={(value) => set('accessAll', value === 'all')}
              >
                <Gapped vertical gap={8}>
                  <Radio<'all' | 'some'> value="all">{f.accessAll}</Radio>
                  <Radio<'all' | 'some'> value="some">{f.accessSome}</Radio>
                </Gapped>
              </RadioGroup>
              {!values.accessAll && (
                <TokenInput<Person>
                  width="100%"
                  aria-label={f.accessSome}
                  error={Boolean(errors.access)}
                  type={TokenInputType.WithReference}
                  getItems={findUsers}
                  selectedItems={values.users}
                  renderItem={person}
                  renderToken={(user, props) => (
                    <Token key={user.id} {...props}>
                      {person(user)}
                    </Token>
                  )}
                  valueToString={person}
                  itemToId={(user) => user.id}
                  disabled={pending}
                  onValueChange={(items) => set('users', items)}
                />
              )}
              {errors.access && (
                <p className={styles.error} role="alert">
                  {errors.access}
                </p>
              )}
              <p className={styles.note}>{f.accessNote}</p>
            </fieldset>

            <section className={styles.group}>
              <h3 ref={check} tabIndex={-1} className={styles.legend}>
                {f.check}
              </h3>
              {saved ? (
                <RightsCheck
                  connection={saved}
                  checking={pending}
                  resultRef={result}
                  loadingSnapshot={loadingSnapshot}
                  onLoadSnapshot={() => void loadSnapshot()}
                />
              ) : (
                <p className={styles.note}>{texts.db.rights.notChecked}</p>
              )}
            </section>
          </form>
        </SidePage.Container>
      </SidePage.Body>
      <SidePage.Footer panel>
        <Gapped gap={8}>
          <Button
            use="primary"
            type="submit"
            form="db-connection-form"
            disabled={pending}
            icon={pending ? <Spinner type="mini" caption={null} /> : undefined}
          >
            {pending ? f.saving : f.save}
          </Button>
          <Button disabled={pending} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </SidePage.Footer>
    </SidePage>
  );
}
