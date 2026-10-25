import { IconPlusRegular16 } from '@skbkontur/icons/IconPlusRegular16';
import { IconTrashCanRegular16 } from '@skbkontur/icons/IconTrashCanRegular16';
import { Button, Checkbox, Gapped, Hint, Input, Link, Select, Textarea } from '@skbkontur/react-ui';
import { useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, ApiError, isApiError } from '../api/client';
import type { CodeParameterType } from '../api/types';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import type { CreatedState } from './CodeHome';
import { useCode } from './CodeContext';
import { ProjectNameField } from './ProjectNameField';
import {
  DEFAULT_ICON,
  emptyParam,
  emptyTool,
  paramNameFrom,
  wizardErrors,
  wizardForm,
  wizardServerError,
  type DraftParam,
  type DraftTool,
  type WizardDraft,
} from './rules';
import { SectionLayout } from './SectionLayout';
import styles from './Wizard.module.css';

const w = texts.workshop;
const tw = w.wizard;
const OTHER_ICON = '__other';
/** Счётчик символов описания появляется с 80 % предела (§5.13.4). */
const COUNTER_FROM = 0.8;

/** Черновик формы живёт, пока открыта вкладка: уход в другой раздел его не теряет (Д-55). */
let stored: WizardDraft | null = null;

function freshDraft(): WizardDraft {
  return { name: '', tools: [emptyTool()] };
}

type Errors = Record<string, string>;

/** Значение поля формы по имени поля контракта (`tools.0.inputs.1.name`). */
function valueAt(draft: WizardDraft, field: string): string {
  let value: unknown = draft;
  for (const part of field.split('.')) {
    value = value && typeof value === 'object' ? (value as Record<string, unknown>)[part] : undefined;
  }
  return JSON.stringify(value ?? null);
}

interface ParamRowProps {
  param: DraftParam;
  at: string;
  index: number;
  input: boolean;
  types: CodeParameterType[];
  errors: Errors;
  disabled: boolean;
  onChange: (next: DraftParam) => void;
  onRemove: () => void;
}

function ParamRow({ param, at, index, input, types, errors, disabled, onChange, onRemove }: ParamRowProps) {
  const error = (key: string) => errors[`${at}.${key}`] ?? null;
  const items = types.map((type) => [type.id, type.title, type.csharp] as [string, string, string]);
  const label = tw.removeParam(param.name || String(index + 1));
  return (
    <div className={styles.paramRow} role="group" aria-label={`${input ? tw.inputs : tw.outputs}: ${index + 1}`}>
      <div data-field={`${at}.name`}>
        <Field label={tw.paramName} hideLabel error={error('name')}>
          {(control) => (
            <Input
              {...control}
              width="100%"
              className="p-mono"
              spellCheck={false}
              value={param.name}
              disabled={disabled}
              onValueChange={(name) => onChange({ ...param, name, nameTouched: true })}
            />
          )}
        </Field>
      </div>
      <Select<string, string>
        width="100%"
        aria-label={tw.paramType}
        items={items}
        value={param.type}
        disabled={disabled}
        renderValue={(value) => {
          const type = types.find((item) => item.id === value);
          return type ? `${type.title} — ${type.csharp}` : value;
        }}
        onValueChange={(type) => onChange({ ...param, type })}
      />
      <div className={styles.required}>
        {input && (
          <Checkbox
            aria-label={tw.requiredLabel(index + 1)}
            checked={param.required ?? false}
            disabled={disabled}
            onValueChange={(required) => onChange({ ...param, required })}
          />
        )}
      </div>
      <div data-field={`${at}.title_ru`}>
        <Field label={tw.labelRu} hideLabel error={error('title_ru')}>
          {(control) => (
            <Input
              {...control}
              width="100%"
              placeholder={tw.labelRuPlaceholder}
              value={param.title_ru}
              disabled={disabled}
              onValueChange={(title_ru) => onChange({ ...param, title_ru })}
            />
          )}
        </Field>
      </div>
      <div data-field={`${at}.title_en`}>
        <Field label={tw.labelEn} hideLabel error={error('title_en')}>
          {(control) => (
            <Input
              {...control}
              width="100%"
              placeholder={tw.labelEnPlaceholder}
              value={param.title_en}
              disabled={disabled}
              onValueChange={(title_en) =>
                // Имя подставляется из подписи по-английски, пока сотрудник не менял его сам.
                onChange({ ...param, title_en, ...(param.nameTouched ? {} : { name: paramNameFrom(title_en) }) })
              }
            />
          )}
        </Field>
      </div>
      <Hint text={label}>
        <Button use="text" icon={<IconTrashCanRegular16 />} aria-label={label} disabled={disabled} onClick={onRemove} />
      </Hint>
    </div>
  );
}

interface ToolCardProps {
  tool: DraftTool;
  index: number;
  count: number;
  errors: Errors;
  disabled: boolean;
  types: CodeParameterType[];
  maxParameters: number;
  maxDescription: number;
  onChange: (next: DraftTool) => void;
  onRemove: () => void;
}

function ToolCard(props: ToolCardProps) {
  const { tool, index, errors, disabled, types } = props;
  const at = `tools.${index}`;
  const titleId = useId();
  const [limitFor, setLimitFor] = useState<'inputs' | 'outputs' | null>(null);
  const error = (key: string) => errors[`${at}.${key}`] ?? null;
  const set = (patch: Partial<DraftTool>) => props.onChange({ ...tool, ...patch });
  const iconItems = [
    ...tw.icons.map(([value, title]) => [value, title, value] as [string, string, string]),
    [OTHER_ICON, tw.otherIcon] as [string, string],
  ];

  const addParam = (group: 'inputs' | 'outputs') => {
    if (tool.inputs.length + tool.outputs.length >= props.maxParameters) {
      setLimitFor(group);
      return;
    }
    setLimitFor(null);
    const allowed = types.filter((type) => (group === 'inputs' ? type.input : type.output));
    set({ [group]: [...tool[group], emptyParam(allowed[0]?.id ?? 'string', group === 'inputs')] });
  };

  const group = (key: 'inputs' | 'outputs') => {
    const allowed = types.filter((type) => (key === 'inputs' ? type.input : type.output));
    return (
      <div className={styles.params}>
        <h4 className={styles.paramsTitle}>{key === 'inputs' ? tw.inputs : tw.outputs}</h4>
        {tool[key].length > 0 && (
          <div className={styles.paramHead} aria-hidden="true">
            <Hint text={tw.paramNameHint}>
              <span>{tw.paramName}</span>
            </Hint>
            <span>{tw.paramType}</span>
            <span>
              {key === 'inputs' && (
                <Hint text={tw.requiredHint}>
                  <span>{tw.required}</span>
                </Hint>
              )}
            </span>
            <span>{tw.labelRu}</span>
            <span>{tw.labelEn}</span>
            <span />
          </div>
        )}
        {tool[key].map((param, p) => (
          <ParamRow
            key={param.key}
            param={param}
            at={`${at}.${key}.${p}`}
            index={p}
            input={key === 'inputs'}
            types={allowed}
            errors={errors}
            disabled={disabled}
            onChange={(next) => set({ [key]: tool[key].map((item) => (item.key === param.key ? next : item)) })}
            onRemove={() => {
              setLimitFor(null);
              set({ [key]: tool[key].filter((item) => item.key !== param.key) });
            }}
          />
        ))}
        <div>
          <Button size="small" icon={<IconPlusRegular16 />} disabled={disabled} onClick={() => addParam(key)}>
            {key === 'inputs' ? tw.addInput : tw.addOutput}
          </Button>
          {limitFor === key && (
            <p className={styles.error} role="alert">
              {tw.errors.maxParams(props.maxParameters)}
            </p>
          )}
        </div>
      </div>
    );
  };

  const counterShown = tool.description.length >= props.maxDescription * COUNTER_FROM;
  return (
    <section className={styles.tool} aria-labelledby={titleId}>
      <div className={styles.toolHead}>
        <h3 id={titleId} className={styles.toolTitle}>
          {tw.tool(index + 1)}
        </h3>
        {props.count > 1 && (
          <Link component="button" disabled={disabled} onClick={props.onRemove}>
            {tw.removeTool}
          </Link>
        )}
      </div>
      <div className={styles.pair}>
        <div data-field={`${at}.title_ru`}>
          <Field label={tw.titleRu} error={error('title_ru')}>
            {(control) => (
              <Input {...control} width="100%" value={tool.title_ru} disabled={disabled} onValueChange={(title_ru) => set({ title_ru })} />
            )}
          </Field>
        </div>
        <div data-field={`${at}.title_en`}>
          <Field label={tw.titleEn} error={error('title_en')}>
            {(control) => (
              <Input {...control} width="100%" value={tool.title_en} disabled={disabled} onValueChange={(title_en) => set({ title_en })} />
            )}
          </Field>
        </div>
      </div>
      <p className={styles.muted}>{tw.titleNote}</p>
      <div className={styles.pair}>
        <div data-field={`${at}.category`}>
          <Field label={tw.category} error={error('category')}>
            {(control) => (
              <Hint text={tw.categoryHint} pos="top left">
                <Input
                  {...control}
                  width="100%"
                  placeholder={tw.categoryPlaceholder}
                  value={tool.category}
                  disabled={disabled}
                  onValueChange={(category) => set({ category })}
                />
              </Hint>
            )}
          </Field>
        </div>
        <div className={styles.iconField} data-field={`${at}.icon`}>
          <Field label={tw.icon}>
            {(control) => (
              <Select<string, string>
                id={control.id}
                width="100%"
                items={iconItems}
                value={tool.customIcon ? OTHER_ICON : tool.icon}
                disabled={disabled}
                renderValue={(value, item) => (value === OTHER_ICON ? tw.otherIcon : tw.iconItem(item ?? value, value))}
                onValueChange={(value) =>
                  set(value === OTHER_ICON ? { customIcon: true, icon: '' } : { customIcon: false, icon: value || DEFAULT_ICON })
                }
              />
            )}
          </Field>
          {tool.customIcon && (
            <Field label={tw.iconClass} hideLabel error={error('icon')}>
              {(control) => (
                <Input
                  {...control}
                  width="100%"
                  className="p-mono"
                  placeholder={tw.iconClass}
                  value={tool.icon}
                  disabled={disabled}
                  onValueChange={(icon) => set({ icon })}
                />
              )}
            </Field>
          )}
        </div>
      </div>
      {group('inputs')}
      {group('outputs')}
      <div className={styles.database}>
        <Checkbox checked={tool.uses_database} disabled={disabled} onValueChange={(uses_database) => set({ uses_database })}>
          {tw.database}
        </Checkbox>
        <p className={styles.muted}>{tw.databaseNote}</p>
      </div>
      <div data-field={`${at}.description`}>
        <Field
          label={tw.description}
          error={error('description')}
          hint={
            <>
              {tw.descriptionHint}
              {counterShown && (
                <span className={tool.description.length > props.maxDescription ? styles.counterOver : styles.counter}>
                  {tw.counter(tool.description.length, props.maxDescription)}
                </span>
              )}
            </>
          }
        >
          {(control) => (
            <Textarea
              {...control}
              width="100%"
              autoResize
              rows={4}
              maxRows={12}
              value={tool.description}
              disabled={disabled}
              onValueChange={(description) => set({ description })}
            />
          )}
        </Field>
      </div>
    </section>
  );
}

/** Мастер «Новый GPTool» — `/code/new` (концепция §5.13.4). */
export function Wizard() {
  const navigate = useNavigate();
  const { config } = useSession();
  const { touchProject, sectionOff } = useCode();
  const [draft, setDraftState] = useState<WizardDraft>(() => stored ?? freshDraft());
  const [submitted, setSubmitted] = useState(false);
  /** Отказы сервера по полям — со значением поля на момент отказа: исправленное поле ошибку теряет. */
  const [server, setServer] = useState<Record<string, { text: string; value: string }>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [toolsLimit, setToolsLimit] = useState(false);
  const [pending, setPending] = useState(false);
  const [focusRequest, setFocusRequest] = useState(0);
  const form = useRef<HTMLDivElement>(null);
  const nameInput = useRef<Input>(null);
  usePageTitle(w.wizardTitle);

  const code = config?.code;
  const maxDescription = config?.dialogs.message_max_chars ?? Number.MAX_SAFE_INTEGER;
  const types = code?.wizard.parameter_types ?? [];
  const serverErrors = Object.fromEntries(
    Object.entries(server)
      .filter(([field, error]) => valueAt(draft, field) === error.value)
      .map(([field, error]) => [field, error.text]),
  );
  const errors: Errors = { ...(submitted ? wizardErrors(draft, maxDescription) : {}), ...serverErrors };

  const setDraft = (next: WizardDraft) => {
    stored = next;
    setDraftState(next);
  };

  // Фокус — в первое поле с ошибкой, по порядку обхода (§5.13.4).
  useEffect(() => {
    if (focusRequest === 0) {
      return;
    }
    const fields = form.current?.querySelectorAll<HTMLElement>('[data-field]') ?? [];
    const first = [...fields].find((element) => element.querySelector('[role="alert"]'));
    first?.querySelector<HTMLElement>('input, textarea, button')?.focus();
  }, [focusRequest]);

  const submit = async () => {
    setSubmitted(true);
    setNotice(null);
    if (Object.keys(wizardErrors(draft, maxDescription)).length > 0) {
      setFocusRequest((n) => n + 1);
      return;
    }
    setPending(true);
    try {
      const created = await api.createCodeProject({ source: 'wizard', wizard: wizardForm(draft) });
      stored = null;
      touchProject(created.project);
      const state: CreatedState = { assistantRequest: created.assistant_request };
      navigate(`/code/${created.project.id}`, { state });
    } catch (error) {
      setPending(false);
      if (sectionOff(error) || isApiError(error, 'unauthenticated')) {
        return;
      }
      if (isApiError(error, 'project_name_taken')) {
        setServer({ name: { text: w.name.taken, value: valueAt(draft, 'name') } });
        setFocusRequest((n) => n + 1);
      } else if (isApiError(error, 'project_limit_reached')) {
        setNotice(w.limitReached(Number(error.details.max)));
      } else if (error instanceof ApiError && error.fields.length > 0) {
        const mapped: Record<string, { text: string; value: string }> = {};
        const unplaced: string[] = [];
        for (const field of error.fields) {
          const { field: name, text } = wizardServerError(field.field, field.code, draft, maxDescription);
          if (text) {
            mapped[name] = { text, value: valueAt(draft, name) };
          } else {
            unplaced.push(field.message);
          }
        }
        setServer(mapped);
        setNotice(unplaced.length > 0 ? unplaced.join(' ') : null);
        setFocusRequest((n) => n + 1);
      } else {
        setNotice(errorText(error));
      }
    }
  };

  const cancel = () => {
    stored = null;
    navigate('/code');
  };

  const addTool = () => {
    if (code && draft.tools.length >= code.wizard.max_tools) {
      setToolsLimit(true);
      return;
    }
    setToolsLimit(false);
    setDraft({ ...draft, tools: [...draft.tools, emptyTool()] });
  };

  return (
    <SectionLayout title={w.wizardTitle} crumb>
      <div ref={form} className={styles.form}>
        <section className={styles.part}>
          <h2 className={styles.partTitle}>{tw.project}</h2>
          <div data-field="name">
            <ProjectNameField
              value={draft.name}
              error={errors.name ?? null}
              disabled={pending}
              inputRef={nameInput}
              onChange={(name) => setDraft({ ...draft, name })}
            />
          </div>
        </section>
        <section className={styles.part}>
          <h2 className={styles.partTitle}>{tw.tools}</h2>
          {draft.tools.map((tool, index) => (
            <ToolCard
              key={tool.key}
              tool={tool}
              index={index}
              count={draft.tools.length}
              errors={errors}
              disabled={pending}
              types={types}
              maxParameters={code?.wizard.max_parameters ?? Number.MAX_SAFE_INTEGER}
              maxDescription={maxDescription}
              onChange={(next) => setDraft({ ...draft, tools: draft.tools.map((item) => (item.key === tool.key ? next : item)) })}
              onRemove={() => {
                setToolsLimit(false);
                setDraft({ ...draft, tools: draft.tools.filter((item) => item.key !== tool.key) });
              }}
            />
          ))}
          <div>
            <Button icon={<IconPlusRegular16 />} disabled={pending} onClick={addTool}>
              {tw.addTool}
            </Button>
            {toolsLimit && code && (
              <p className={styles.error} role="alert">
                {tw.errors.maxTools(code.wizard.max_tools)}
              </p>
            )}
          </div>
        </section>
        {notice && <Notice kind="error">{notice}</Notice>}
        <div className={styles.submit}>
          <Gapped gap={8}>
            <Button use="primary" loading={pending} onClick={() => void submit()}>
              {tw.submit}
            </Button>
            <Button disabled={pending} onClick={cancel}>
              {texts.common.cancel}
            </Button>
          </Gapped>
          <p className={styles.muted}>{tw.footer}</p>
        </div>
      </div>
    </SectionLayout>
  );
}
