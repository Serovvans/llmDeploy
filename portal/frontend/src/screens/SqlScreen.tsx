import { Button, Hint, Link, MenuHeader, Select, SingleToast, Switcher, Toggle, Tooltip } from '@skbkontur/react-ui';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { DbConnection, DbTableRef, Memory, SqlAction, SqlSchemaSummary } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { DialogScreen } from '../components/chat/DialogScreen';
import { ConfigNotice } from '../components/ConfigNotice';
import { Notice } from '../components/Notice';
import { TitleHint } from '../components/TitleHint';
import { connectionOf, dbValue, NO_SCHEMA, schemaItems, selectionLabel, snapshotDay } from '../db/selection';
import { TablePickerSide } from '../db/TablePicker';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { memoryQuery } from '../memory/useDialogMemory';
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
/** Отметки таблиц сохраняются одним запросом после паузы; дольше — «Сохраняю отметки…» (§5.7). */
const SAVE_DELAY_MS = 300;
const d = texts.db;

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
  /** Подпись и пояснение: с подключением с образцами — «Проверять на образце из базы» (§5.7). */
  label: string;
  hint: string;
}

/**
 * «Проверять на тестовых данных» (концепция §5.7). Недоступный переключатель показан выключенным,
 * остаётся в обходе по Tab и объясняет причину при наведении, фокусе и нажатии.
 */
function TestRunToggle({ checked, unavailable, onChange, label, hint }: TestRunToggleProps) {
  const descriptionId = useId();
  const description = (
    <span id={descriptionId} className="p-visually-hidden">
      {unavailable ?? hint}
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
            aria-label={label}
            aria-describedby={descriptionId}
          >
            <span className={styles.unavailableToggleBody} aria-hidden="true">
              <Toggle size="small" checked={false} disabled />
              {label}
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
        <Hint text={hint} maxWidth={HINT_MAX_PX}>
          {label}
        </Hint>
      </Toggle>
      {description}
    </>
  );
}

/** Недоступный список «Диалект» с подключением: в обходе по Tab, пояснение по наведению и фокусу (§5.7, Д-70). */
function FixedDialect({ title }: { title: string }) {
  return (
    <TitleHint text={d.dialectFixed} always>
      <span className={styles.fixedDialect} tabIndex={0} role="button" aria-disabled="true" aria-label={`${t.dialect}: ${title}`}>
        <span className={styles.unavailableToggleBody} aria-hidden="true">
          <Select<string, string> size="small" items={[[title, title]]} value={title} disabled />
        </span>
      </span>
    </TitleHint>
  );
}

type Selection = { connection_id: string; tables: DbTableRef[] } | null;

/** SQL-помощник (концепция §5.7): действие, диалект, схема и проверка относятся к отправляемому вопросу. */
export function SqlScreen() {
  const { config, configFailed, reloadConfig } = useSession();
  const { id } = useParams();
  const navigate = useNavigate();
  const chat = useDialogs('sql');
  const { regenerate, createDialog, showDb, newKey } = chat;
  const view = id ? chat.state.dialogs[id] : undefined;
  const [action, setAction] = useState(storedAction);
  const [dialectChoice, setDialectChoice] = useState(() => stored(DIALECT_KEY));
  const [schemaChoice, setSchemaChoice] = useState(() => stored(SCHEMA_KEY));
  const [testRunChoice, setTestRunChoice] = useState(() => stored(TEST_RUN_KEY) === 'on');
  const [schemas, setSchemas] = useState<SqlSchemaSummary[] | null>(null);
  const [connections, setConnections] = useState<DbConnection[] | null>(null);
  const [panelOpen, setPanelOpen] = useState(false);
  // Выбор, который сейчас сохраняется у диалога: список уже показывает его (§5.7).
  const [pendingChoice, setPendingChoice] = useState<string | null>(null);
  const [savingSlow, setSavingSlow] = useState(false);
  const [sendWaiting, setSendWaiting] = useState(false);
  const [picker, setPicker] = useState<{ connectionId: string; tables: DbTableRef[] } | null>(null);
  const [pickerReload, setPickerReload] = useState(0);
  const [pickerMemory, setPickerMemory] = useState<Memory | null>(null);
  const rememberOpener = useFocusReturn(panelOpen || picker !== null);
  const saving = useRef<Promise<boolean>>(Promise.resolve(true));
  const saveTimer = useRef<number | null>(null);
  const autoCreated = useRef(false);
  const dbOn = config?.db.enabled === true;

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

  const dbOff = useCallback(() => {
    reloadConfig();
    SingleToast.push(d.refusals.off);
  }, [reloadConfig]);

  const loadConnections = useCallback(() => {
    api.listDbConnections().then(
      ({ items }) => setConnections(items),
      (error: unknown) => {
        if (isApiError(error, 'feature_disabled')) {
          dbOff();
        }
        // Иначе список подключений не получен: в выборе остаются «Мои схемы».
      },
    );
  }, [dbOff]);

  useEffect(() => {
    if (dbOn) {
      loadConnections();
    }
  }, [dbOn, loadConnections]);

  // Запомненные диалект и схема, которых больше нет, заменяются на умолчание и «Без схемы».
  const dialects = config?.sql.dialects ?? [];
  const serverDb = dbOn && view?.status === 'ready' ? view.db : null;
  // Открыт существующий диалог с выбором — список показывает выбор диалога, иначе — запомненный (§5.7).
  const rawChoice =
    pendingChoice ??
    (serverDb
      ? serverDb.available && serverDb.connection_id
        ? dbValue(serverDb.connection_id)
        : NO_SCHEMA
      : (schemaChoice ?? NO_SCHEMA));
  const rawConnection = connectionOf(rawChoice);
  const connection = rawConnection && dbOn ? (connections?.find((item) => item.id === rawConnection) ?? null) : null;
  const schemaId = !rawConnection && schemas?.some((item) => item.id === rawChoice) ? rawChoice : null;
  const choice = connection ? rawChoice : (schemaId ?? NO_SCHEMA);
  const sandboxDialect = config?.sql.test_run.dialect ?? '';
  const dialect = connection
    ? sandboxDialect
    : dialects.some((item) => item.id === dialectChoice)
      ? (dialectChoice as string)
      : (config?.sql.default_dialect ?? '');

  // Подключение пропало: у диалога — «недоступно» от сервера, у запомненного — нет в списке (§5.7).
  let unavailableNotice: string | null = null;
  if (serverDb && !serverDb.available && pendingChoice === null) {
    unavailableNotice = d.unavailableNotice(serverDb.title);
  } else if (dbOn && rawConnection && connections && !connection) {
    unavailableNotice = d.unavailableNoticeUnknown;
  }
  const unusableNotice =
    connection && !connection.usable
      ? connection.snapshot
        ? d.unusableNotice(connection.title, snapshotDay(connection.snapshot.taken_at))
        : d.unusableNoticeNoSnapshot(connection.title)
      : null;

  // Проверка на тестовых данных: одна схема и один проверяемый диалект. Запомненное «включено»
  // не теряется, пока проверка невозможна: вернётся схема — вернётся и положение переключателя.
  const testRun = config?.sql.test_run.enabled ? config.sql.test_run : null;
  let testRunUnavailable: string | null = null;
  if (testRun && dialect !== testRun.dialect) {
    const checked = dialects.find((item) => item.id === testRun.dialect);
    testRunUnavailable = t.testRun.wrongDialect(checked?.title ?? testRun.dialect);
  } else if (schemaId === null && connection === null) {
    testRunUnavailable = dbOn ? d.sql.needSchema : t.testRun.needSchema;
  }
  const testRunOn = testRun !== null && testRunUnavailable === null && testRunChoice;
  const withSamples = connection?.samples_allowed === true;
  const params = { action, dialect, schema_id: schemaId, test_run: testRunOn };
  // Таймеры и ответы сервера берут адрес и параметры на момент срабатывания: диалог мог появиться за это время.
  const latest = useRef({ id, params });
  useEffect(() => {
    latest.current = { id, params };
  });

  const chooseSchema = (value: string | null) => {
    setSchemaChoice(value);
    store(SCHEMA_KEY, value);
  };

  /** Сохранить выбор у диалога; нового диалога ещё нет — создать его (контракт §7.6.9). */
  const persist = useCallback(
    async (selection: Selection): Promise<boolean> => {
      // Сохранения идут цепочкой: следующее берёт диалог, созданный предыдущим.
      let dialogId = latest.current.id;
      if (!dialogId) {
        if (selection === null) {
          return true;
        }
        dialogId = await createDialog(newKey, (created) => navigate(`/sql/${created}`, { replace: true }));
        latest.current = { ...latest.current, id: dialogId };
      }
      const dialog = await api.setDialogDb(dialogId, selection);
      showDb(dialogId, dialog.db ?? null);
      return true;
    },
    [createDialog, navigate, newKey, showDb],
  );

  /** Сохранение выбора с состояниями §5.7: список сразу показывает новый выбор; отказ возвращает сохранённый. */
  const save = useCallback(
    (selection: Selection, display: string | null): Promise<boolean> => {
      const job = saving.current.then(async () => {
        setPendingChoice(display);
        const slow = window.setTimeout(() => setSavingSlow(true), SAVE_DELAY_MS);
        try {
          return await persist(selection);
        } catch (error) {
          if (isApiError(error, 'db_connection_not_found')) {
            loadConnections();
            SingleToast.push(d.selectionGone);
          } else if (isApiError(error, 'db_snapshot_missing') && selection) {
            setPicker({ connectionId: selection.connection_id, tables: [] });
          } else if (isApiError(error, 'validation_error') && error.fields.some((field) => field.field.startsWith('db.tables')) && selection) {
            // Схему обновили в другой вкладке: выбор таблиц перечитывается и снимает отметки с исчезнувших.
            setPicker({ connectionId: selection.connection_id, tables: selection.tables });
            setPickerReload((value) => value + 1);
          } else if (isApiError(error, 'feature_disabled')) {
            dbOff();
          } else if (!isApiError(error, 'unauthenticated')) {
            SingleToast.push(d.selectionFailed, { use: 'error' });
          }
          return false;
        } finally {
          window.clearTimeout(slow);
          setSavingSlow(false);
          setPendingChoice(null);
        }
      });
      saving.current = job;
      return job;
    },
    [dbOff, loadConnections, persist],
  );

  /** Оценка памяти с отмеченными таблицами — для подвала выбора таблиц (§5.7): после каждого сохранения. */
  const memoryOn = config?.memory.enabled === true;
  const estimate = useCallback(() => {
    const { id: dialogId, params: current } = latest.current;
    if (!dialogId || !memoryOn) {
      return;
    }
    api.getMemory(dialogId, memoryQuery(current)).then(setPickerMemory, () => setPickerMemory(null));
  }, [memoryOn]);

  const openPicker = (target: DbConnection) => {
    rememberOpener('[data-opener="tables"] button');
    setPickerMemory(null);
    setPicker({ connectionId: target.id, tables: serverDb?.connection_id === target.id ? serverDb.tables : [] });
    estimate();
  };

  const chooseValue = (value: string) => {
    const nextConnection = connectionOf(value);
    chooseSchema(value || null);
    if (!nextConnection) {
      // Схема или «Без схемы»: выбор подключения у диалога снимается (§5.7).
      if (id && view?.db) {
        void save(null, value);
      }
      return;
    }
    const target = connections?.find((item) => item.id === nextConnection);
    if (!target) {
      return;
    }
    if (!target.snapshot) {
      // Схемы ещё нет: сразу выбор таблиц в состоянии «Схемы ещё нет», без попытки сохранить.
      openPicker(target);
      return;
    }
    const tables = serverDb?.connection_id === nextConnection ? serverDb.tables : [];
    autoCreated.current = true;
    void save({ connection_id: nextConnection, tables }, value).then((ok) => ok && estimate());
  };

  // Запомненное подключение у нового диалога: диалог создаётся сразу при открытии экрана (А-132).
  const remembered = !id && dbOn && connection?.snapshot ? connection.id : null;
  useEffect(() => {
    if (!remembered || autoCreated.current) {
      return;
    }
    autoCreated.current = true;
    void save({ connection_id: remembered, tables: [] }, dbValue(remembered));
  }, [remembered, save]);
  useEffect(() => {
    if (!id) {
      autoCreated.current = false;
    }
  }, [id]);

  // Отметки таблиц: сохранение у диалога одним запросом через 300 мс после последней отметки.
  const changeTables = (tables: DbTableRef[]) => {
    if (!picker) {
      return;
    }
    setPicker({ ...picker, tables });
    if (saveTimer.current !== null) {
      window.clearTimeout(saveTimer.current);
    }
    const connectionId = picker.connectionId;
    saveTimer.current = window.setTimeout(() => {
      saveTimer.current = null;
      void save({ connection_id: connectionId, tables }, dbValue(connectionId)).then((ok) => ok && estimate());
    }, SAVE_DELAY_MS);
  };
  useEffect(
    () => () => {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current);
      }
    },
    [],
  );

  /** Перед вопросом выбор должен быть сохранён у диалога (§5.7): отправка ждёт сохранения. */
  const ensureSaved = async (): Promise<string | null | false> => {
    setSendWaiting(true);
    try {
      if (saveTimer.current !== null) {
        window.clearTimeout(saveTimer.current);
        saveTimer.current = null;
        if (picker) {
          await save({ connection_id: picker.connectionId, tables: picker.tables }, dbValue(picker.connectionId));
        }
      }
      if (!(await saving.current)) {
        return false;
      }
      if (connection) {
        if (id && serverDb?.connection_id === connection.id) {
          return null;
        }
        if (!connection.snapshot) {
          openPicker(connection);
          return false;
        }
        return (await save({ connection_id: connection.id, tables: [] }, choice)) ? null : false;
      }
      // Без подключения, а у диалога оно осталось (в том числе недоступное): сначала снять его.
      if (id && view?.db) {
        return (await save(null, choice)) ? null : false;
      }
      return null;
    } finally {
      setSendWaiting(false);
    }
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

  /** «Ответить заново»: если схема или база вопроса недоступна, один раз повторяем с текущим выбором. */
  const regenerateAnswer = (dialogId: string) => {
    regenerate(dialogId, {
      onRefused: (error) => {
        if (isApiError(error, 'validation_error') && error.fields.some((field) => field.field === 'dialect')) {
          SingleToast.push(d.otherDialect);
          return true;
        }
        if (isApiError(error, 'db_connection_unavailable')) {
          SingleToast.push(d.regenerateReplaced);
          regenerate(dialogId, { body: { schema_id: schemaId } });
          return true;
        }
        if (!isApiError(error, 'schema_not_found')) {
          return false;
        }
        void loadSchemas().then((items) => {
          const current = items?.some((item) => item.id === schemaChoice) ? schemaChoice : null;
          SingleToast.push(t.schemaReplaced);
          regenerate(dialogId, { body: { schema_id: current } });
        });
        return true;
      },
    });
  };

  const pickerConnection = picker ? (connections?.find((item) => item.id === picker.connectionId) ?? null) : null;
  const savedTables = connection && serverDb?.connection_id === connection.id ? serverDb.tables.length : 0;
  const banner =
    !config && configFailed ? (
      <ConfigNotice />
    ) : unavailableNotice || unusableNotice ? (
      <Notice kind="warning">{unavailableNotice ?? unusableNotice}</Notice>
    ) : undefined;

  return (
    <>
      <DialogScreen
        kind="sql"
        basePath="/sql"
        sectionTitle={t.title}
        empty={dbOn ? { title: t.empty.title, text: d.sql.emptyText } : t.empty}
        params={params}
        placeholder={t.placeholders[action]}
        mono={action !== 'write'}
        // Какой из двух текстов верен, известно только с настройками портала.
        note={
          config
            ? dbOn
              ? testRun
                ? d.sql.noteWithRun
                : d.sql.noteWithoutRun
              : testRun
                ? t.note.withRun
                : t.note.withoutRun
            : undefined
        }
        onRegenerate={regenerateAnswer}
        memoryRevision={JSON.stringify(serverDb)}
        onMemorySchemaMissing={() => {
          // Схему удалили в другой вкладке: выбор станет «Без схемы», оценка повторится без неё (концепция §4.9).
          void loadSchemas();
          SingleToast.push(t.schemas.alreadyRemoved);
        }}
        // Первый запрос настроек ещё идёт или сохраняется выбор базы: отказа не было, отправка ждёт.
        sendPending={(!config && !configFailed) || sendWaiting || savingSlow}
        beforeSend={() => {
          if (!config) {
            // Диалект обязателен (контракт §5.3): без настроек вопрос не уходит, их загрузка повторяется сразу.
            reloadConfig();
            return texts.common.configFailed;
          }
          return dbOn ? ensureSaved() : null;
        }}
        onSendRefused={(error) => {
          if (isApiError(error, 'schema_not_found')) {
            void loadSchemas();
          } else if (isApiError(error, 'feature_disabled')) {
            // Проверку выключили на портале, пока экран был открыт: переключатель уйдёт вместе с новыми настройками.
            reloadConfig();
          } else if (isApiError(error, 'db_connection_unavailable') && id) {
            // Доступ закрыли между выбором и отправкой: вопрос остаётся в поле, над лентой — заметка (§5.7).
            loadConnections();
            api.getDialog(id).then(
              (dialog) => showDb(id, dialog.db ?? null),
              () => {
                // Заметка появится при следующем открытии диалога.
              },
            );
            return true;
          }
          return false;
        }}
        banner={banner}
        headerSettings={
          <>
            {(config || !configFailed) &&
              (connection ? (
                <FixedDialect title={dialects.find((item) => item.id === sandboxDialect)?.title ?? sandboxDialect} />
              ) : (
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
              ))}
            <TitleHint text={t.schema} always>
              <Select<string, string>
                size="small"
                maxWidth={SCHEMA_SELECT_MAX_PX}
                aria-label={t.schema}
                items={schemaItems(schemas ?? [], dbOn ? (connections ?? []) : [], t.schema)}
                value={choice}
                renderValue={(value) => selectionLabel(value ?? NO_SCHEMA, schemas ?? [], connections ?? [])}
                onValueChange={chooseValue}
              />
            </TitleHint>
            {connection && (
              <span data-opener="tables">
                <Button size="small" onClick={() => openPicker(connection)}>
                  {savedTables > 0 ? d.tables(savedTables) : d.chooseTables}
                </Button>
              </span>
            )}
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
                label={withSamples ? d.sql.testRunSample : t.testRun.label}
                hint={withSamples ? d.sql.testRunSampleHint : dbOn ? d.sql.testRunHint : t.testRun.hint}
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
      {picker && pickerConnection && config && (
        <TablePickerSide
          connection={pickerConnection}
          selected={picker.tables}
          max={config.db.max_selected_tables}
          reloadKey={pickerReload}
          saving={savingSlow}
          overflow={pickerMemory?.level === 'overflow'}
          footerExtra={
            pickerMemory && (
              <Hint text={texts.memory.percentHint(Math.max(0, Math.round(pickerMemory.percent)))}>
                <span tabIndex={0}>
                  {texts.memory.meterCaption} {texts.memory.levels[pickerMemory.level]}
                </span>
              </Hint>
            )
          }
          onChange={changeTables}
          onSnapshot={() => {
            // Схема загружена: выбор с этим подключением теперь сохраняется у диалога.
            loadConnections();
            if (serverDb?.connection_id !== picker.connectionId) {
              void save({ connection_id: picker.connectionId, tables: picker.tables }, dbValue(picker.connectionId)).then(
                (ok) => ok && estimate(),
              );
            }
          }}
          onFeatureOff={() => {
            setPicker(null);
            dbOff();
          }}
          onClose={() => setPicker(null)}
        />
      )}
    </>
  );
}
