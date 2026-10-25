import { Button, Checkbox, Gapped, Link, Modal, Select, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { CodeProject, CodeSdk, SqlSchemaSummary } from '../api/types';
import { Notice } from '../components/Notice';
import { useCopy } from '../hooks/useCopy';
import { texts } from '../texts';
import styles from './SettingsModal.module.css';

const s = texts.workshop.settings;
/** Сколько библиотек сервера видно до «Показать все» (как на схеме §5.13.13). */
const LIBRARIES_SHOWN = 4;
const NO_SCHEMA = '';

function NameRow({ label, value }: { label: string; value: string }) {
  const { label: copyLabel, copy } = useCopy();
  return (
    <div className={styles.name}>
      <span className={styles.label}>{label}</span>
      <code className={styles.mono}>{value}</code>
      <Link onClick={() => void copy(value)}>{copyLabel}</Link>
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

/** Настройки проекта — `Modal` 640 px (концепция §5.13.13): имена для CoGIS, пакеты, схема базы. */
export function SettingsModal({ project, sdk, onClose, onSaved, onProjectGone, sectionOff }: Props) {
  const [packages, setPackages] = useState<string[]>(project.packages);
  const [schemaId, setSchemaId] = useState<string>(project.schema_id ?? NO_SCHEMA);
  const [schemas, setSchemas] = useState<SqlSchemaSummary[]>([]);
  const [allLibraries, setAllLibraries] = useState(false);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [schemaError, setSchemaError] = useState<string | null>(null);

  const loadSchemas = useCallback(() => {
    api.listSqlSchemas().then(
      ({ items }) => setSchemas(items),
      () => {
        // Без списка в выборе остаются «Без схемы» и текущая схема.
      },
    );
  }, []);

  useEffect(() => {
    loadSchemas();
  }, [loadSchemas]);

  const save = async () => {
    setPending(true);
    setFailure(null);
    setSchemaError(null);
    try {
      const saved = await api.updateCodeProject(project.id, { packages, schema_id: schemaId || null });
      onSaved(saved);
      SingleToast.push(s.saved);
      onClose();
    } catch (error) {
      if (isApiError(error, 'schema_not_found')) {
        // Схему удалили, пока окно открыто: выбор — «Без схемы», окно не закрывается (§5.13.13).
        loadSchemas();
        setSchemaId(NO_SCHEMA);
        setSchemaError(s.schemaGone);
      } else if (isApiError(error, 'not_found')) {
        onProjectGone();
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
  const schemaItems: [string, string][] = [
    [NO_SCHEMA, s.noSchema],
    ...schemas.map((schema): [string, string] => [schema.id, schema.name]),
  ];

  return (
    <Modal width={640} ignoreBackgroundClick disableClose={pending} onClose={onClose}>
      <Modal.Header>{s.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.body}>
          <section className={styles.part}>
            <h2 className={styles.heading}>{s.names}</h2>
            <NameRow label={s.dll} value={`${project.name}.dll`} />
            <NameRow label={s.factory} value={`${project.name}.GpToolFactory`} />
          </section>
          <section className={styles.part}>
            <h2 className={styles.heading}>{s.packages}</h2>
            {sdk?.available ? (
              <p className={styles.muted}>
                {s.libraries(shown.map(libraryName).join(', '))}
                {hidden > 0 && s.librariesMore(hidden)}
                {hidden > 0 && (
                  <>
                    {' '}
                    <Link onClick={() => setAllLibraries(true)}>{s.showAll}</Link>
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
          <section className={styles.part}>
            <h2 className={styles.heading} id="code-settings-schema">
              {s.schema}
            </h2>
            <div>
              <Select<string>
                aria-labelledby="code-settings-schema"
                width={320}
                items={schemaItems}
                value={schemaId}
                error={schemaError !== null}
                disabled={pending}
                onValueChange={(value) => {
                  setSchemaId(value);
                  setSchemaError(null);
                }}
              />
              {schemaError && (
                <p className={styles.error} role="alert">
                  {schemaError}
                </p>
              )}
            </div>
            <p className={styles.muted}>{s.schemasNote}</p>
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
