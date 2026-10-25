import { Button, FileUploader, Gapped, Input, Modal } from '@skbkontur/react-ui';
import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, ApiError, isApiError } from '../api/client';
import type { CodeConfig, CodeCreated } from '../api/types';
import { Notice } from '../components/Notice';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, formatSize, texts } from '../texts';
import styles from './Code.module.css';
import { useCode } from './CodeContext';
import { ProjectNameField } from './ProjectNameField';
import { nameFromArchive, projectNameError, projectNameServerError } from './rules';
import { SectionLayout } from './SectionLayout';

const w = texts.workshop;

interface Failure {
  field: string | null;
  notice: string | null;
}

/** Отказ создания проекта (таблица §5.13.15): имя — под полем, остальное — в окне. */
export function createFailure(error: unknown, name: string, config: CodeConfig | null): Failure {
  if (!(error instanceof ApiError)) {
    return { field: null, notice: errorText(error) };
  }
  if (error.code === 'project_name_taken') {
    return { field: w.name.taken, notice: null };
  }
  const field = error.fields.find((item) => item.field === 'name' || item.field === 'wizard.name');
  const fieldText = field ? projectNameServerError(field.code, name) : null;
  if (fieldText) {
    return { field: fieldText, notice: null };
  }
  const notices: Record<string, string> = {
    project_limit_reached: w.limitReached(Number(error.details.max)),
    unsupported_file_type: w.zip.notZip,
    file_too_large: w.zip.tooLarge(formatSize(config?.import_max_bytes ?? Number(error.details.max_bytes))),
    file_unreadable: w.zip.unreadable,
    import_no_files: w.zip.noFiles,
  };
  return { field: null, notice: notices[error.code] ?? errorText(error) };
}

/** Состояние окна создания: отправка, ошибка поля имени, заметка в окне. */
function useCreate(onCreated: (created: CodeCreated) => void) {
  const { config } = useSession();
  const { sectionOff } = useCode();
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Failure>({ field: null, notice: null });
  const nameInput = useRef<Input>(null);

  const run = async (name: string, request: () => Promise<CodeCreated>, before?: () => string | null) => {
    const problem = projectNameError(name);
    const other = before?.() ?? null;
    if (problem || other) {
      setFailure({ field: problem, notice: other });
      if (problem) {
        nameInput.current?.focus();
      }
      return;
    }
    setPending(true);
    setFailure({ field: null, notice: null });
    try {
      onCreated(await request());
    } catch (error) {
      setPending(false);
      if (!sectionOff(error) && !isApiError(error, 'unauthenticated')) {
        const next = createFailure(error, name, config?.code ?? null);
        setFailure(next);
        if (next.field) {
          nameInput.current?.focus();
        }
      }
    }
  };
  return { pending, failure, setFailure, nameInput, run, config: config?.code ?? null };
}

function ExampleModal({ onClose, onCreated }: { onClose: () => void; onCreated: (created: CodeCreated) => void }) {
  const [name, setName] = useState<string>(w.example.defaultName);
  const create = useCreate(onCreated);

  return (
    <Modal width={560} ignoreBackgroundClick disableClose={create.pending} onClose={onClose}>
      <Modal.Header>{w.example.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.modalBody}>
          <p>{w.example.text}</p>
          <ProjectNameField
            value={name}
            error={create.failure.field}
            disabled={create.pending}
            inputRef={create.nameInput}
            onChange={(value) => {
              setName(value);
              create.setFailure({ field: null, notice: null });
            }}
          />
          {create.failure.notice && <Notice kind="error">{create.failure.notice}</Notice>}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button
            use="primary"
            loading={create.pending}
            onClick={() => void create.run(name, () => api.createCodeProject({ source: 'example', name }))}
          >
            {w.example.submit}
          </Button>
          <Button disabled={create.pending} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}

function ImportModal({ onClose, onCreated }: { onClose: () => void; onCreated: (created: CodeCreated) => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState('');
  const uploader = useRef<FileUploader>(null);
  const create = useCreate(onCreated);
  const limit = create.config ? formatSize(create.config.import_max_bytes) : null;

  return (
    <Modal width={560} ignoreBackgroundClick disableClose={create.pending} onClose={onClose}>
      <Modal.Header>{w.zip.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.modalBody}>
          <div className={styles.uploader}>
            <FileUploader
              ref={uploader}
              hideFiles
              width="100%"
              accept=".zip"
              uploaderText={w.zip.uploader}
              disabled={create.pending}
              onAttach={(attached) => {
                const chosen = attached[0]?.originalFile ?? null;
                uploader.current?.reset();
                if (chosen) {
                  setFile(chosen);
                  // Имя подставляется из имени архива, если подходит под правила (§5.13.3).
                  setName(nameFromArchive(chosen.name));
                  create.setFailure({ field: null, notice: null });
                }
              }}
            />
            {file && <p className={styles.fileName}>{file.name}</p>}
            {limit && <p className={styles.muted}>{w.zip.note(limit)}</p>}
          </div>
          <ProjectNameField
            value={name}
            error={create.failure.field}
            disabled={create.pending}
            inputRef={create.nameInput}
            onChange={(value) => {
              setName(value);
              create.setFailure((failure) => ({ ...failure, field: null }));
            }}
          />
          {create.failure.notice && <Notice kind="error">{create.failure.notice}</Notice>}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button
            use="primary"
            loading={create.pending}
            onClick={() =>
              void create.run(
                name,
                () => api.importCodeProject(file as File, name),
                () => (file ? null : w.zip.chooseFile),
              )
            }
          >
            {w.zip.submit}
          </Button>
          <Button disabled={create.pending} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}

/** Состояние, которое экран проекта получает при переходе после создания (§5.13.3, §5.13.4). */
export interface CreatedState {
  assistantRequest?: string | null;
  importReport?: CodeCreated['import'];
}

/** Начало раздела `/code` (концепция §5.13.3): три способа создать проект. */
export function CodeHome() {
  const navigate = useNavigate();
  const { touchProject } = useCode();
  const [modal, setModal] = useState<'example' | 'zip' | null>(null);
  const rememberOpener = useFocusReturn(modal !== null);
  const openModal = (kind: 'example' | 'zip') => {
    rememberOpener(`[data-opener="${kind}"] button`);
    setModal(kind);
  };
  usePageTitle(w.title);

  const opened = (created: CodeCreated) => {
    touchProject(created.project);
    const state: CreatedState = { importReport: created.import };
    navigate(`/code/${created.project.id}`, { state });
  };

  const blocks = [
    { ...w.home.wizard, opener: 'wizard', primary: true, onClick: () => navigate('/code/new') },
    { ...w.home.example, opener: 'example', primary: false, onClick: () => openModal('example') },
    { ...w.home.zip, opener: 'zip', primary: false, onClick: () => openModal('zip') },
  ];

  return (
    <SectionLayout title={w.title}>
      <div className={styles.column}>
        <p className={styles.intro}>{w.home.intro}</p>
        <div className={styles.blocks}>
          {blocks.map((block) => (
            <section key={block.title} className={styles.block}>
              <h2 className={styles.blockTitle}>{block.title}</h2>
              <p className={styles.blockText}>{block.text}</p>
              <div data-opener={block.opener}>
                <Button use={block.primary ? 'primary' : 'default'} onClick={block.onClick}>
                  {block.action}
                </Button>
              </div>
            </section>
          ))}
        </div>
        <p className={styles.muted}>{w.home.footer}</p>
      </div>
      {modal === 'example' && <ExampleModal onClose={() => setModal(null)} onCreated={opened} />}
      {modal === 'zip' && <ImportModal onClose={() => setModal(null)} onCreated={opened} />}
    </SectionLayout>
  );
}
