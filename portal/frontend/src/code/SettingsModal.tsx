import { Button, Checkbox, Gapped, Link, Modal, Select, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useState } from 'react';

import { api, isApiError } from '../api/client';
import type {
  CatalogClient,
  CatalogInsertion,
  CatalogPlacement,
  CodeProject,
  CodeSdk,
  DbConnection,
  DbTableRef,
  SqlSchemaSummary,
} from '../api/types';
import { Notice } from '../components/Notice';
import { connectionOf, dbValue, NO_SCHEMA, schemaItems, selectionLabel } from '../db/selection';
import { TablePickerBody } from '../db/TablePicker';
import { useCopy } from '../hooks/useCopy';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import { kindOf, kindTexts } from './kinds';
import styles from './SettingsModal.module.css';

const s = texts.workshop.settings;
const ws = texts.workshop.web.settings;
/** Сколько библиотек сервера видно до «Показать все» (как на схеме §5.13.13). */
const LIBRARIES_SHOWN = 4;
const INSERTIONS: CatalogInsertion[] = ['head_start', 'head_end', 'body_start', 'body_end'];
const CLIENTS: CatalogClient[] = ['smartphone', 'tablet', 'desktop'];

/** Настройки файла без сохранённых: `.css` — конец `<head>`, `.js` и `.html` — конец `<body>`, все клиенты (§5.13.23). */
function placementOf(file: CodeProject['files'][number]): CatalogPlacement {
  return file.catalog ?? { insertion: file.path.toLowerCase().endsWith('.css') ? 'head_end' : 'body_end', clients: CLIENTS };
}

function NameRow({ label, value }: { label: string; value: string }) {
  const { label: copyLabel, copy } = useCopy();
  return (
    <div className={styles.name}>
      <span className={styles.label}>{label}</span>
      <code className={styles.mono}>{value}</code>
      <Link component="button" onClick={() => void copy(value)}>{copyLabel}</Link>
    </div>
  );
}

function libraryName(library: CodeSdk['libraries'][number]): string {
  const name = library.file_name.replace(/\.dll$/i, '');
  return library.version ? `${name} ${library.version}` : name;
}

interface Props {
  project: CodeProject;
  sdk: CodeSdk | null;
  onClose: () => void;
  onSaved: (project: CodeProject) => void;
  onProjectGone: () => void;
  sectionOff: (error: unknown) => boolean;
}

/** Настройки проекта — `Modal` 640 px (концепция §5.13.13, §5.13.23): вид, имена для CoGIS, пакеты, схема или база, Каталог. */
export function SettingsModal({ project, sdk, onClose, onSaved, onProjectGone, sectionOff }: Props) {
  const { config, reloadConfig } = useSession();
  const kind = kindOf(project);
  const gptool = kind === 'gptool';
  const dbOn = config?.db.enabled === true;
  const [packages, setPackages] = useState<string[]>(project.packages);
  // Недоступное подключение показано как «Без схемы» с заметкой; сохранять с ним нельзя (§5.13.23).
  const [choice, setChoice] = useState<string>(() =>
    project.db?.available && project.db.connection_id ? dbValue(project.db.connection_id) : (project.schema_id ?? NO_SCHEMA),
  );
  const [tables, setTables] = useState<DbTableRef[]>(project.db?.tables ?? []);
  const [catalog, setCatalog] = useState<Record<string, CatalogPlacement>>(() =>
    Object.fromEntries(project.files.map((file) => [file.id, placementOf(file)])),
  );
  const [schemas, setSchemas] = useState<SqlSchemaSummary[]>([]);
  const [connections, setConnections] = useState<DbConnection[]>([]);
  const [allLibraries, setAllLibraries] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [catalogTried, setCatalogTried] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const unavailable = dbOn && project.db && !project.db.available ? project.db.title : null;

  const loadSchemas = useCallback(() => {
    api.listSqlSchemas().then(
      ({ items }) => setSchemas(items),
      () => {
        // Без списка в выборе остаются «Без схемы» и текущая схема.
      },
    );
  }, []);

  const loadConnections = useCallback(() => {
    if (!dbOn) {
      return;
    }
    api.listDbConnections().then(
      ({ items }) => setConnections(items),
      () => {
        // Без списка подключений — только «Мои схемы».
      },
    );
  }, [dbOn]);

  useEffect(() => {
    loadSchemas();
    loadConnections();
  }, [loadConnections, loadSchemas]);

  const dbOff = () => {
    reloadConfig();
    SingleToast.push(texts.db.refusals.off);
  };

  const connectionId = dbOn ? connectionOf(choice) : null;
  const connection = connections.find((item) => item.id === connectionId) ?? null;
  const noClient = (fileId: string) => (catalog[fileId]?.clients.length ?? 0) === 0;
  const catalogInvalid = kind === 'catalog' && project.files.some((file) => noClient(file.id));

  const save = async () => {
    setCatalogTried(true);
    if (catalogInvalid) {
      return;
    }
    setPending(true);
    setFailure(null);
    setSchemaError(null);
    // Отправляется только поле текущего выбора: схема и подключение снимают друг друга (контракт §7.6.9, А-145).
    const selection = connectionId
      ? { db: { connection_id: connectionId, tables } }
      : choice
        ? { schema_id: choice }
        : dbOn
          ? { schema_id: null, db: null }
          : { schema_id: null };
    try {
      const saved = await api.updateCodeProject(project.id, {
        ...(gptool ? { packages } : {}),
        ...selection,
        ...(kind === 'catalog'
          ? { catalog_files: project.files.map((file) => ({ file_id: file.id, ...(catalog[file.id] ?? placementOf(file)) })) }
          : {}),
      });
      onSaved(saved);
      SingleToast.push(s.saved);
      onClose();
    } catch (error) {
      if (isApiError(error, 'schema_not_found')) {
        // Схему удалили, пока окно открыто: выбор — «Без схемы», окно не закрывается (§5.13.13).
        loadSchemas();
        setChoice(NO_SCHEMA);
        setSchemaError(s.schemaGone);
      } else if (isApiError(error, 'db_connection_not_found')) {
        loadConnections();
        setChoice(NO_SCHEMA);
        setSchemaError(texts.db.unavailableProject(connection?.title ?? project.db?.title ?? ''));
      } else if (isApiError(error, 'db_snapshot_missing')) {
        // Встроенный выбор таблиц покажет «Схема этой базы ещё не загружена».
        setReloadKey((value) => value + 1);
      } else if (isApiError(error, 'validation_error') && error.fields.some((field) => field.field.startsWith('db.tables'))) {
        // Таблицы нет в схеме: её обновили в другой вкладке — выбор перечитывается и снимает отметку (§5.13.24).
        setReloadKey((value) => value + 1);
      } else if (isApiError(error, 'not_found')) {
        onProjectGone();
      } else if (isApiError(error, 'feature_disabled') && 'db' in selection) {
        dbOff();
      } else if (sectionOff(error)) {
        onClose();
      } else if (!isApiError(error, 'unauthenticated')) {
        setFailure(s.failed);
      }
    } finally {
      setPending(false);
    }
  };

  const libraries = sdk?.libraries ?? [];
  const shown = allLibraries ? libraries : libraries.slice(0, LIBRARIES_SHOWN);
  const hidden = libraries.length - shown.length;
  const offline = sdk?.packages ?? [];
  const kindText = kindTexts(kind);

  return (
    <Modal width={640} ignoreBackgroundClick disableClose={pending} onClose={onClose}>
      <Modal.Header>{s.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.body}>
          <div className={styles.name}>
            <span className={styles.label}>{ws.kind}</span>
            <span>{ws.kindValue(kindText.title, kindText.where)}</span>
          </div>
          {gptool && (
            <section className={styles.part}>
              <h2 className={styles.heading}>{s.names}</h2>
              <NameRow label={s.dll} value={`${project.name}.dll`} />
              <NameRow label={s.factory} value={`${project.name}.GpToolFactory`} />
            </section>
          )}
          {gptool && (
            <section className={styles.part}>
              <h2 className={styles.heading}>{s.packages}</h2>
              {sdk?.available ? (
                <p className={styles.muted}>
                  {s.libraries(shown.map(libraryName).join(', '))}
                  {hidden > 0 && s.librariesMore(hidden)}
                  {hidden > 0 && (
                    <>
                      {' '}
                      <Link component="button" onClick={() => setAllLibraries(true)}>{s.showAll}</Link>
                    </>
                  )}
                </p>
              ) : (
                <p className={styles.muted}>{s.noSdk}</p>
              )}
              <h3 className={styles.subheading}>{s.fromSet}</h3>
              {offline.length === 0 ? (
                <p className={styles.muted}>{s.noPackages}</p>
              ) : (
                <div className={styles.packages}>
                  {offline.map((item) => (
                    <Checkbox
                      key={item.id}
                      disabled={item.on_server || pending}
                      checked={!item.on_server && packages.includes(item.id)}
                      onValueChange={(checked) =>
                        setPackages((current) => (checked ? [...current, item.id] : current.filter((id) => id !== item.id)))
                      }
                    >
                      <span className={styles.mono}>
                        {item.id} {item.version}
                      </span>
                      {item.on_server && <span className={styles.muted}>{s.onServer}</span>}
                    </Checkbox>
                  ))}
                </div>
              )}
              <p className={styles.muted}>{s.missingPackage}</p>
            </section>
          )}
          {kind === 'catalog' && (
            <section className={styles.part}>
              <h2 className={styles.heading}>{ws.catalog}</h2>
              {project.files.map((file) => {
                const placement = catalog[file.id] ?? placementOf(file);
                const update = (next: Partial<CatalogPlacement>) =>
                  setCatalog((current) => ({ ...current, [file.id]: { ...placement, ...next } }));
                return (
                  <div key={file.id} className={styles.catalogFile}>
                    <code className={styles.mono}>{file.path}</code>
                    <div className={styles.catalogControls}>
                      <Select<CatalogInsertion>
                        width={180}
                        aria-label={`${ws.insertion}: ${file.path}`}
                        items={INSERTIONS.map((value): [CatalogInsertion, string] => [value, ws.insertions[value]])}
                        value={placement.insertion}
                        disabled={pending}
                        onValueChange={(insertion) => update({ insertion })}
                      />
                      <Gapped gap={12}>
                        {CLIENTS.map((client) => (
                          <Checkbox
                            key={client}
                            checked={placement.clients.includes(client)}
                            disabled={pending}
                            error={catalogTried && noClient(file.id)}
                            onValueChange={(checked) =>
                              update({
                                clients: checked
                                  ? CLIENTS.filter((item) => item === client || placement.clients.includes(item))
                                  : placement.clients.filter((item) => item !== client),
                              })
                            }
                          >
                            {ws.clients[client]}
                          </Checkbox>
                        ))}
                      </Gapped>
                    </div>
                    {catalogTried && noClient(file.id) && (
                      <p className={styles.error} role="alert">
                        {ws.noClient}
                      </p>
                    )}
                  </div>
                );
              })}
            </section>
          )}
          <section className={styles.part}>
            <h2 className={styles.heading} id="code-settings-schema">
              {s.schema}
            </h2>
            {unavailable !== null && choice === NO_SCHEMA && !schemaError && (
              <Notice kind="warning">{texts.db.unavailableProject(unavailable)}</Notice>
            )}
            <div>
              <Select<string, string>
                aria-labelledby="code-settings-schema"
                width={320}
                items={schemaItems(schemas, connections)}
                value={choice}
                renderValue={(value) => selectionLabel(value ?? NO_SCHEMA, schemas, connections)}
                error={schemaError !== null}
                disabled={pending}
                onValueChange={(value) => {
                  if (connectionOf(value) !== connectionOf(choice)) {
                    setTables([]);
                  }
                  setChoice(value);
                  setSchemaError(null);
                }}
              />
              {schemaError && (
                <p className={styles.error} role="alert">
                  {schemaError}
                </p>
              )}
            </div>
            <p className={styles.muted}>{dbOn ? ws.schemasNoteDb : s.schemasNote}</p>
            {connection && config && (
              <TablePickerBody
                key={connection.id}
                connection={connection}
                selected={tables}
                max={config.db.max_selected_tables}
                reloadKey={reloadKey}
                onChange={setTables}
                onFeatureOff={dbOff}
              />
            )}
          </section>
          {failure && <Notice kind="error">{failure}</Notice>}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={pending} onClick={() => void save()}>
            {s.save}
          </Button>
          <Button disabled={pending} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
