import { Hint, Link, MenuHeader, Select, SingleToast, Switcher, Toggle, Tooltip } from '@skbkontur/react-ui';
import { useCallback, useEffect, useId, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { SqlAction, SqlSchemaSummary } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { DialogScreen } from '../components/chat/DialogScreen';
import { ConfigNotice } from '../components/ConfigNotice';
import { TitleHint } from '../components/TitleHint';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { useSession } from '../session/SessionContext';
import { SchemasPanel } from '../sql/SchemasPanel';
import { texts } from '../texts';
import styles from './SqlScreen.module.css';

const t = texts.sql;
const ACTION_KEY = 'portal.sql.action';
const DIALECT_KEY = 'portal.sql.dialect';
const SCHEMA_KEY = 'portal.sql.schema';
const TEST_RUN_KEY = 'portal.sql.test_run';
const ACTIONS = (Object.keys(t.actions) as SqlAction[]).map((value) => ({ value, label: t.actions[value] }));
const SCHEMA_SELECT_MAX_PX = 180;
const HINT_MAX_PX = 320;

function stored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    // Хранилище недоступно — значения по умолчанию.
    return null;
  }
}

function store(key: string, value: string | null): void {
  try {
    if (value === null) {
      window.localStorage.removeItem(key);
    } else {
      window.localStorage.setItem(key, value);
    }
  } catch {
    // Хранилище недоступно — выбор действует до закрытия вкладки.
  }
}

function storedAction(): SqlAction {
  const value = stored(ACTION_KEY);
  return ACTIONS.some((item) => item.value === value) ? (value as SqlAction) : 'write';
}

/** Название поля без подписи — первой строкой раскрытого списка (концепция §5.7). */
function menuTitle(title: string) {
  return Select.staticElement(() => <MenuHeader>{title}</MenuHeader>);
}

interface TestRunToggleProps {
  checked: boolean;
  /** Почему проверка сейчас невозможна; `null` — переключатель доступен. */
  unavailable: string | null;
  onChange: (checked: boolean) => void;
}

/**
 * «Проверять на тестовых данных» (концепция §5.7). Недоступный переключатель показан выключенным,
 * остаётся в обходе по Tab и объясняет причину при наведении, фокусе и нажатии.
 */
function TestRunToggle({ checked, unavailable, onChange }: TestRunToggleProps) {
  const descriptionId = useId();
  const description = (
    <span id={descriptionId} className="p-visually-hidden">
      {unavailable ?? t.testRun.hint}
    </span>
  );
  if (unavailable !== null) {
    return (
      <>
        <Tooltip trigger="hover&focus" pos="bottom right" render={() => <div className={styles.reason}>{unavailable}</div>}>
          <span
            className={styles.unavailableToggle}
            tabIndex={0}
            role="switch"
            aria-checked="false"
            aria-disabled="true"
            aria-label={t.testRun.label}
            aria-describedby={descriptionId}
          >
            <span className={styles.unavailableToggleBody} aria-hidden="true">
              <Toggle size="small" checked={false} disabled />
              {t.testRun.label}
            </span>
          </span>
        </Tooltip>
        {description}
      </>
    );
  }
  return (
    <>
      <Toggle size="small" checked={checked} onValueChange={onChange} aria-describedby={descriptionId}>
        <Hint text={t.testRun.hint} maxWidth={HINT_MAX_PX}>
          {t.testRun.label}
        </Hint>
      </Toggle>
      {description}
    </>
  );
}

/** SQL-помощник (концепция §5.7): действие, диалект, схема и проверка относятся к отправляемому вопросу. */
export function SqlScreen() {
  const { config, configFailed, reloadConfig } = useSession();
  const { regenerate } = useDialogs('sql');
  const [action, setAction] = useState(storedAction);
  const [dialectChoice, setDialectChoice] = useState(() => stored(DIALECT_KEY));
  const [schemaChoice, setSchemaChoice] = useState(() => stored(SCHEMA_KEY));
  const [testRunChoice, setTestRunChoice] = useState(() => stored(TEST_RUN_KEY) === 'on');
  const [schemas, setSchemas] = useState<SqlSchemaSummary[] | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  const rememberOpener = useFocusReturn(panelOpen);

  const loadSchemas = useCallback(async (): Promise<SqlSchemaSummary[] | null> => {
    try {
      const { items } = await api.listSqlSchemas();
      setSchemas(items);
      return items;
    } catch {
      // Список не получен: остаётся прежний, вопрос можно задать без схемы.
      return null;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    api.listSqlSchemas().then(
      ({ items }) => {
        if (!cancelled) {
          setSchemas(items);
        }
      },
      () => {
        // Список не получен: вопрос можно задать без схемы.
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // Запомненные диалект и схема, которых больше нет, заменяются на умолчание и «Без схемы».
  const dialects = config?.sql.dialects ?? [];
  const dialect = dialects.some((item) => item.id === dialectChoice)
    ? (dialectChoice as string)
    : (config?.sql.default_dialect ?? '');
  const schemaId = schemas?.some((item) => item.id === schemaChoice) ? schemaChoice : null;

  // Проверка на тестовых данных: одна схема и один проверяемый диалект. Запомненное «включено»
  // не теряется, пока проверка невозможна: вернётся схема — вернётся и положение переключателя.
  const testRun = config?.sql.test_run.enabled ? config.sql.test_run : null;
  let testRunUnavailable: string | null = null;
  if (testRun && dialect !== testRun.dialect) {
    const checked = dialects.find((item) => item.id === testRun.dialect);
    testRunUnavailable = t.testRun.wrongDialect(checked?.title ?? testRun.dialect);
  } else if (schemaId === null) {
    testRunUnavailable = t.testRun.needSchema;
  }
  const testRunOn = testRun !== null && testRunUnavailable === null && testRunChoice;

  const chooseSchema = (id: string | null) => {
    setSchemaChoice(id);
    store(SCHEMA_KEY, id);
  };

  const onSchemasChanged = useCallback(
    (selectId?: string) => {
      void loadSchemas();
      if (selectId) {
        setSchemaChoice(selectId);
        store(SCHEMA_KEY, selectId);
      }
    },
    [loadSchemas],
  );

  /** «Ответить заново»: если схема вопроса удалена, один раз повторяем с текущим выбором схемы. */
  const regenerateAnswer = (id: string) => {
    regenerate(id, {
      onRefused: (error) => {
        if (!isApiError(error, 'schema_not_found')) {
          return false;
        }
        void loadSchemas().then((items) => {
          const current = items?.some((item) => item.id === schemaChoice) ? schemaChoice : null;
          SingleToast.push(t.schemaReplaced);
          regenerate(id, { body: { schema_id: current } });
        });
        return true;
      },
    });
  };

  return (
    <>
      <DialogScreen
        kind="sql"
        basePath="/sql"
        sectionTitle={t.title}
        empty={t.empty}
        params={{ action, dialect, schema_id: schemaId, test_run: testRunOn }}
        placeholder={t.placeholders[action]}
        mono={action !== 'write'}
        // Какой из двух текстов верен, известно только с настройками портала.
        note={config ? (testRun ? t.note.withRun : t.note.withoutRun) : undefined}
        onRegenerate={regenerateAnswer}
        // Первый запрос настроек ещё идёт: отказа не было, отправка ждёт ответа.
        sendPending={!config && !configFailed}
        beforeSend={() => {
          if (config) {
            return null;
          }
          // Диалект обязателен (контракт §5.3): без настроек вопрос не уходит, их загрузка повторяется сразу.
          reloadConfig();
          return texts.common.configFailed;
        }}
        onSendRefused={(error) => {
          if (isApiError(error, 'schema_not_found')) {
            void loadSchemas();
          } else if (isApiError(error, 'feature_disabled')) {
            // Проверку выключили на портале, пока экран был открыт: переключатель уйдёт вместе с новыми настройками.
            reloadConfig();
          }
          return false;
        }}
        banner={!config && configFailed ? <ConfigNotice /> : undefined}
        headerSettings={
          <>
            {(config || !configFailed) && (
              <TitleHint text={t.dialect} always>
                <Select<string, string>
                  size="small"
                  aria-label={t.dialect}
                  items={[menuTitle(t.dialect), ...dialects.map((item) => [item.id, item.title] as [string, string])]}
                  value={dialect}
                  onValueChange={(value) => {
                    setDialectChoice(value);
                    store(DIALECT_KEY, value);
                  }}
                />
              </TitleHint>
            )}
            <TitleHint text={t.schema} always>
              <Select<string, string>
                size="small"
                maxWidth={SCHEMA_SELECT_MAX_PX}
                aria-label={t.schema}
                items={[
                  menuTitle(t.schema),
                  ['', t.noSchema],
                  ...(schemas ?? []).map((item) => [item.id, item.name] as [string, string]),
                ]}
                value={schemaId ?? ''}
                onValueChange={(value) => chooseSchema(value || null)}
              />
            </TitleHint>
            <span data-opener="schemas">
              <Link
                component="button"
                onClick={() => {
                  rememberOpener('[data-opener="schemas"] button');
                  setPanelOpen(true);
                }}
              >
                {t.mySchemas}
              </Link>
            </span>
            {testRun && (
              <TestRunToggle
                checked={testRunChoice}
                unavailable={testRunUnavailable}
                onChange={(checked) => {
                  setTestRunChoice(checked);
                  store(TEST_RUN_KEY, checked ? 'on' : null);
                }}
              />
            )}
          </>
        }
        composerControls={
          <Switcher
            size="small"
            items={ACTIONS}
            value={action}
            onValueChange={(value) => {
              setAction(value as SqlAction);
              store(ACTION_KEY, value);
            }}
          />
        }
      />
      {panelOpen && (
        <SchemasPanel
          schemas={schemas ?? []}
          limits={config?.sql ?? null}
          onChanged={onSchemasChanged}
          onClose={() => setPanelOpen(false)}
        />
      )}
    </>
  );
}
