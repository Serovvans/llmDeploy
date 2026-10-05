import { Link, Select, SingleToast, Switcher } from '@skbkontur/react-ui';
import { useCallback, useEffect, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { SqlAction, SqlSchemaSummary } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { DialogScreen } from '../components/chat/DialogScreen';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { useSession } from '../session/SessionContext';
import { SchemasPanel } from '../sql/SchemasPanel';
import { texts } from '../texts';
import styles from './ToolScreen.module.css';

const t = texts.sql;
const DIALECT_KEY = 'portal.sql.dialect';
const SCHEMA_KEY = 'portal.sql.schema';
const ACTIONS = (Object.keys(t.actions) as SqlAction[]).map((value) => ({ value, label: t.actions[value] }));

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

/** SQL-помощник (концепция §5.7): действие, диалект и схема относятся к отправляемому вопросу. */
export function SqlScreen() {
  const { config } = useSession();
  const { regenerate } = useDialogs('sql');
  const [action, setAction] = useState<SqlAction>('write');
  const [dialectChoice, setDialectChoice] = useState(() => stored(DIALECT_KEY));
  const [schemaChoice, setSchemaChoice] = useState(() => stored(SCHEMA_KEY));
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
        params={{ action, dialect, schema_id: schemaId }}
        placeholder={t.placeholders[action]}
        mono={action !== 'write'}
        note={t.note}
        onRegenerate={regenerateAnswer}
        onSendRefused={(error) => {
          if (isApiError(error, 'schema_not_found')) {
            void loadSchemas();
          }
          return false;
        }}
        toolbar={
          <div className={styles.toolbar}>
            <Switcher items={ACTIONS} value={action} onValueChange={(value) => setAction(value as SqlAction)} />
            <div className={styles.settings}>
              <label className={styles.setting}>
                <span>{t.dialect}</span>
                <Select<string, string>
                  items={dialects.map((item) => [item.id, item.title] as [string, string])}
                  value={dialect}
                  onValueChange={(value) => {
                    setDialectChoice(value);
                    store(DIALECT_KEY, value);
                  }}
                />
              </label>
              <label className={styles.setting}>
                <span>{t.schema}</span>
                <Select<string, string>
                  items={[['', t.noSchema], ...(schemas ?? []).map((item) => [item.id, item.name] as [string, string])]}
                  value={schemaId ?? ''}
                  onValueChange={(value) => chooseSchema(value || null)}
                />
              </label>
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
            </div>
          </div>
        }
      />
      {panelOpen && config && (
        <SchemasPanel
          schemas={schemas ?? []}
          limits={config.sql}
          onChanged={onSchemasChanged}
          onClose={() => setPanelOpen(false)}
        />
      )}
    </>
  );
}
