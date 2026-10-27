import { IconNetDownloadRegular16 } from '@skbkontur/icons/IconNetDownloadRegular16';
import { Button, Gapped, Link, Loader, Modal, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useState } from 'react';

import { api, isApiError } from '../../api/client';
import type { CodeFile, CodeProject } from '../../api/types';
import { Notice } from '../../components/Notice';
import { StepList } from '../../components/StepList';
import { useCopy } from '../../hooks/useCopy';
import { errorText, texts } from '../../texts';
import archiveStyles from '../ArchiveModal.module.css';
import { saveBlob } from '../download';
import { kindOf, type WebKind } from '../kinds';
import { loadMonaco } from '../monaco/loader';
import styles from './WebExportModal.module.css';
import { catalogTypeOf, lineCount } from './webRules';

const e = texts.workshop.web.exportModal;
/** «Скопировать код» → «Скопировано» (§5.13.22): копируется сохранённый файл целиком. */
function CopyCode({ path, read }: { path: string; read: (path: string) => Promise<string | null> }) {
  const { label, copy } = useCopy();
  return (
    <Link
      component="button"
      aria-label={`${e.copy}: ${path}`}
      onClick={() => {
        void read(path).then((content) => (content === null ? undefined : copy(content)));
      }}
    >
      {label === texts.common.copy ? e.copy : label}
    </Link>
  );
}

function CatalogBlock({ file, read }: { file: CodeFile; read: (path: string) => Promise<string | null> }) {
  const placement = file.catalog;
  const all = !placement || placement.clients.length === 3;
  const clients = all ? e.catalogAll : (placement?.clients ?? []).map((client) => e.catalogClientNames[client]).join(', ');
  return (
    <div className={styles.catalogBlock}>
      <p className={archiveStyles.mono}>{file.path}</p>
      <p>{e.catalogType(e.catalogTypes[catalogTypeOf(file.path)])}</p>
      <p>{e.catalogPlace(e.catalogInsertions[placement?.insertion ?? 'body_end'])}</p>
      <p>{e.catalogClients(clients)}</p>
      <p>
        {e.catalogCode} <CopyCode path={file.path} read={read} />
      </p>
    </div>
  );
}

interface Props {
  project: CodeProject | { id: string };
  /** Ошибок синтаксиса; не передано — окно проверит файлы само (открыто из списка проектов). */
  syntaxErrors?: number;
  /** Несохранённая правка сначала сохраняется; отдаёт сохранённые файлы. */
  savedFiles?: () => Promise<CodeFile[]>;
  onClose: () => void;
  sectionOff: (error: unknown) => boolean;
}

/** «Скачать для CoGIS» у JS и HTML (концепция §5.13.22): окно и `Памятка.txt` строятся из одних данных. */
export function WebExportModal({ project: given, syntaxErrors, savedFiles, onClose, sectionOff }: Props) {
  const [project, setProject] = useState<CodeProject | null>('files' in given ? given : null);
  const [errors, setErrors] = useState<number | null>(syntaxErrors ?? null);
  const [loadFailed, setLoadFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const projectId = given.id;

  useEffect(() => {
    if (project) {
      return;
    }
    let cancelled = false;
    api.getCodeProject(projectId).then(
      (loaded) => {
        if (!cancelled) {
          setProject(loaded);
        }
      },
      (error: unknown) => {
        if (!cancelled && !sectionOff(error)) {
          setLoadFailed(true);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [project, projectId, sectionOff, attempt]);

  // Открыто из списка: ошибки синтаксиса считаются по сохранённым файлам тем же правилом, что в редакторе.
  useEffect(() => {
    if (syntaxErrors !== undefined || !project) {
      return;
    }
    let cancelled = false;
    loadMonaco()
      .then((setup) => setup.countSyntaxErrors(project.files))
      .then(
        (count) => {
          if (!cancelled) {
            setErrors(count);
          }
        },
        () => {
          // Без редактора число ошибок неизвестно: окно работает без второго предупреждения.
        },
      );
    return () => {
      cancelled = true;
    };
  }, [project, syntaxErrors]);

  const read = async (path: string): Promise<string | null> => {
    try {
      const files = savedFiles ? await savedFiles() : (project?.files ?? []);
      return files.find((file) => file.path === path)?.content ?? null;
    } catch (error) {
      SingleToast.push(errorText(error), { use: 'error' });
      return null;
    }
  };

  const download = async () => {
    setPending(true);
    setFailure(null);
    try {
      if (savedFiles) {
        await savedFiles();
      }
      const { blob, fileName } = await api.exportCodeProject(projectId);
      saveBlob(blob, fileName ?? `${project?.name ?? 'project'}.zip`);
    } catch (error) {
      if (sectionOff(error)) {
        onClose();
      } else if (!isApiError(error, 'unauthenticated')) {
        setFailure(errorText(error));
      }
    } finally {
      setPending(false);
    }
  };

  let body: React.ReactNode;
  if (!project) {
    body = loadFailed ? (
      <Notice
        kind="error"
        action={{
          label: texts.common.retry,
          onClick: () => {
            setLoadFailed(false);
            setAttempt((n) => n + 1);
          },
        }}
      >
        {texts.common.actionFailed}
      </Notice>
    ) : (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else {
    const kind = kindOf(project) as WebKind;
    const files = [...project.files].sort((a, b) => a.path.localeCompare(b.path));
    const main = files[0]?.path ?? '';
    const titles = kind === 'catalog' ? e.steps.catalog : e.steps[kind](main);
    const steps = titles.map((title, index) => ({
      title,
      children:
        kind === 'catalog' && index === 1 ? (
          <div className={styles.catalog}>
            {files.map((file) => (
              <CatalogBlock key={file.id} file={file} read={read} />
            ))}
          </div>
        ) : null,
    }));
    const note = kind === 'js_plugin' || kind === 'card_hooks' ? e.notes[kind] : null;
    body = (
      <div className={archiveStyles.body}>
        <Notice kind="warning">{e.warning[kind]}</Notice>
        {errors !== null && errors > 0 && <Notice kind="warning">{e.syntax(errors)}</Notice>}
        <section>
          <h2 className={archiveStyles.heading}>{e.code}</h2>
          <ul className={styles.files}>
            {files.map((file) => (
              <li key={file.id} className={styles.file}>
                <span>
                  <span className={archiveStyles.mono}>{file.path}</span>
                  <span className={archiveStyles.muted}> · {e.lines(lineCount(file.content))}</span>
                </span>
                <CopyCode path={file.path} read={read} />
              </li>
            ))}
          </ul>
        </section>
        <section className={archiveStyles.steps}>
          <h2 className={archiveStyles.heading}>{e.howTo}</h2>
          <StepList steps={steps} />
          {note && <p className={`${archiveStyles.muted} ${styles.note}`}>{note}</p>}
        </section>
        {failure && <Notice kind="error">{failure}</Notice>}
      </div>
    );
  }

  return (
    <Modal width={640} ignoreBackgroundClick onClose={onClose}>
      <Modal.Header>{e.title}</Modal.Header>
      <Modal.Body>{body}</Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button
            use="primary"
            icon={<IconNetDownloadRegular16 />}
            loading={pending}
            disabled={!project}
            onClick={() => void download()}
          >
            {e.download}
          </Button>
          <Button onClick={onClose}>{e.close}</Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
