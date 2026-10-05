import { Button, Gapped, Hint, Input, Kebab, Loader, MenuItem, SidePage, SingleToast, Textarea } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { SqlSchemaSummary } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { errorText, formatDate, texts } from '../texts';
import styles from './SchemasPanel.module.css';

const t = texts.sql.schemas;
const NAME_MAX = 100;
const ADD_OPENER = '[data-opener="schema-add"] button';
const menuOpener = (id: string) => `[data-opener="schema-${id}"] [tabindex="0"]`;

interface SchemasPanelProps {
  schemas: SqlSchemaSummary[];
  /** Пределы из настроек; `null` — настройки не загрузились, пределы проверяет только сервер. */
  limits: { schema_max_chars: number; max_schemas: number } | null;
  /** Список изменился: его пора перечитать; `selectId` — схема, которую выбрать в «Схема базы». */
  onChanged: (selectId?: string) => void;
  onClose: () => void;
}

/** `null` — список; `{ id: null }` — новая схема; иначе правка существующей. */
type Editing = { id: string | null; name: string; content: string; loading: boolean } | null;

/** «Мои схемы» (концепция §5.7): список и форма в одной боковой панели. */
export function SchemasPanel({ schemas, limits, onChanged, onClose }: SchemasPanelProps) {
  const [editing, setEditing] = useState<Editing>(null);
  const [errors, setErrors] = useState<{ name: string | null; content: string | null }>({ name: null, content: null });
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [removing, setRemoving] = useState<SqlSchemaSummary | null>(null);
  const [removePending, setRemovePending] = useState(false);
  // Окно удаления и форма — тоже окна: после них фокус возвращается в список (концепция §9).
  const rememberOpener = useFocusReturn(editing !== null || removing !== null);
  const nameRef = useRef<Input>(null);
  const contentRef = useRef<Textarea>(null);

  // Фокус — в первое поле с ошибкой.
  useEffect(() => {
    if (errors.name) {
      nameRef.current?.focus();
    } else if (errors.content) {
      contentRef.current?.focus();
    }
  }, [errors]);

  const editingId = editing?.loading ? editing.id : null;
  // «Изменить» сначала запрашивает схему целиком: в списке текста схемы нет.
  useEffect(() => {
    if (!editingId) {
      return;
    }
    let cancelled = false;
    api.getSqlSchema(editingId).then(
      (schema) => {
        if (!cancelled) {
          setEditing({ id: schema.id, name: schema.name, content: schema.content, loading: false });
        }
      },
      (error: unknown) => {
        if (cancelled) {
          return;
        }
        setEditing(null);
        SingleToast.push(isApiError(error, 'not_found') ? t.alreadyRemoved : errorText(error), { use: 'error' });
        onChanged();
      },
    );
    return () => {
      cancelled = true;
    };
  }, [editingId, onChanged]);

  const openForm = (next: NonNullable<Editing>) => {
    rememberOpener(next.id ? menuOpener(next.id) : ADD_OPENER);
    setErrors({ name: null, content: null });
    setFormError(null);
    setEditing(next);
  };

  const save = async (event: React.SyntheticEvent) => {
    event.preventDefault();
    if (!editing || saving) {
      return;
    }
    const name = editing.name.trim();
    const checked = {
      name: !name ? t.enterName : name.length > NAME_MAX ? t.nameTooLong : null,
      content: !editing.content.trim()
        ? t.enterContent
        : limits && editing.content.length > limits.schema_max_chars
          ? t.contentTooLong(limits.schema_max_chars)
          : null,
    };
    setErrors(checked);
    setFormError(null);
    if (checked.name || checked.content) {
      return;
    }
    setSaving(true);
    try {
      const body = { name, content: editing.content };
      const saved = editing.id ? await api.updateSqlSchema(editing.id, body) : await api.createSqlSchema(body);
      SingleToast.push(t.saved);
      setEditing(null);
      onChanged(saved.id);
    } catch (error) {
      if (isApiError(error, 'schema_name_taken')) {
        setErrors({ name: t.nameTaken, content: null });
      } else if (limits && isApiError(error, 'schema_limit_reached')) {
        setFormError(t.limitReached(limits.max_schemas));
      } else if (
        limits &&
        isApiError(error, 'validation_error') &&
        error.fields.some((f) => f.field === 'content' && f.code === 'too_long')
      ) {
        setErrors({ name: null, content: t.contentTooLong(limits.schema_max_chars) });
      } else if (isApiError(error, 'not_found')) {
        SingleToast.push(t.alreadyRemoved, { use: 'error' });
        setEditing(null);
        onChanged();
      } else if (!isApiError(error, 'unauthenticated')) {
        setFormError(errorText(error));
      }
    } finally {
      setSaving(false);
    }
  };

  const confirmRemove = async () => {
    if (!removing) {
      return;
    }
    setRemovePending(true);
    try {
      await api.deleteSqlSchema(removing.id);
      SingleToast.push(t.removed);
      // Строки больше нет: фокус — на кнопку добавления.
      rememberOpener(ADD_OPENER);
    } catch (error) {
      SingleToast.push(isApiError(error, 'not_found') ? t.alreadyRemoved : errorText(error), { use: 'error' });
    }
    setRemovePending(false);
    setRemoving(null);
    onChanged();
  };

  return (
    <SidePage width={640} blockBackground ignoreOutsideClick onClose={onClose}>
      <SidePage.Header>
        {t.title}
        <span className={styles.privacy}>{t.privacy}</span>
      </SidePage.Header>
      <SidePage.Body>
        <SidePage.Container>
          {editing ? (
            <Loader active={editing.loading} caption={texts.common.loading} delayBeforeSpinnerShow={300}>
              <form className={styles.form} onSubmit={save} noValidate>
                {formError && <Notice kind="error">{formError}</Notice>}
                <Field label={t.name} error={errors.name}>
                  {(control) => (
                    <Input
                      {...control}
                      ref={nameRef}
                      width="100%"
                      autoFocus
                      value={editing.name}
                      onValueChange={(name) => setEditing({ ...editing, name })}
                      disabled={saving || editing.loading}
                    />
                  )}
                </Field>
                <Field label={t.content} error={errors.content} hint={t.contentHint}>
                  {(control) => (
                    <Textarea
                      {...control}
                      ref={contentRef}
                      width="100%"
                      rows={16}
                      className="p-mono"
                      spellCheck={false}
                      value={editing.content}
                      onValueChange={(content) => setEditing({ ...editing, content })}
                      disabled={saving || editing.loading}
                    />
                  )}
                </Field>
                <Gapped gap={8}>
                  <Button type="submit" use="primary" loading={saving} disabled={editing.loading}>
                    {t.save}
                  </Button>
                  <Button disabled={saving} onClick={() => setEditing(null)}>
                    {texts.common.cancel}
                  </Button>
                </Gapped>
              </form>
            </Loader>
          ) : (
            <div className={styles.list}>
              <div data-opener="schema-add">
                <Button onClick={() => openForm({ id: null, name: '', content: '', loading: false })}>{t.add}</Button>
              </div>
              {schemas.length === 0 ? (
                <p className="p-muted">{t.empty}</p>
              ) : (
                <ul className={styles.items}>
                  {schemas.map((schema) => (
                    <li key={schema.id} className={styles.item}>
                      <span className={styles.name}>{schema.name}</span>
                      <span className={styles.date}>{formatDate(schema.updated_at)}</span>
                      <span data-opener={`schema-${schema.id}`}>
                        <Hint text={texts.users.actions} pos="left">
                          <Kebab aria-label={`${texts.users.actions}: ${schema.name}`}>
                            <MenuItem onClick={() => openForm({ id: schema.id, name: schema.name, content: '', loading: true })}>
                              {t.edit}
                            </MenuItem>
                            <MenuItem
                              onClick={() => {
                                rememberOpener(menuOpener(schema.id));
                                setRemoving(schema);
                              }}
                            >
                              {t.remove}
                            </MenuItem>
                          </Kebab>
                        </Hint>
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </SidePage.Container>
      </SidePage.Body>
      {removing && (
        <ConfirmModal
          title={t.removeTitle(removing.name)}
          body={t.removeBody}
          action={t.remove}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </SidePage>
  );
}
