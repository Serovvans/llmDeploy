import { Button, FileUploader, Gapped, Input, Link, Modal } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { api, ApiError, isApiError } from '../api/client';
import type { CodeConfig, CodeCreated } from '../api/types';
import { Field } from '../components/Field';
import { Notice } from '../components/Notice';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, formatSize, texts } from '../texts';
import styles from './Code.module.css';
import { useCode } from './CodeContext';
import { KINDS, kindTexts, type WebKind } from './kinds';
import { ProjectNameField } from './ProjectNameField';
import { nameFromArchive, PROJECT_NAME_MAX, projectNameError, projectNameServerError } from './rules';
import { SectionLayout } from './SectionLayout';

const w = texts.workshop;

export interface Failure {
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

/** Окно создания из шаблона веб-вида (§5.13.18): название — свободный текст до 64 символов. */
function TemplateModal({ kind, onClose, onCreated }: { kind: WebKind; onClose: () => void; onCreated: (created: CodeCreated) => void }) {
  const { config } = useSession();
  const { sectionOff } = useCode();
  const [name, setName] = useState('');
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Failure>({ field: null, notice: null });
  const nameInput = useRef<Input>(null);
  const kindText = kindTexts(kind);
  const n = w.web.name;

  const submit = async () => {
    const value = name.trim();
    const problem = !value ? n.enter : value.length > PROJECT_NAME_MAX ? n.tooLong : null;
    if (problem) {
      setFailure({ field: problem, notice: null });
      nameInput.current?.focus();
      return;
    }
    setPending(true);
    setFailure({ field: null, notice: null });
    try {
      onCreated(await api.createCodeProject({ source: 'template', kind, name: value }));
    } catch (error) {
      setPending(false);
      if (sectionOff(error) || isApiError(error, 'unauthenticated')) {
        return;
      }
      const next = templateFailure(error, config?.code ?? null);
      setFailure(next);
      if (next.field) {
        nameInput.current?.focus();
      }
    }
  };

  return (
    <Modal width={560} ignoreBackgroundClick disableClose={pending} onClose={onClose}>
      <Modal.Header>{kindText.modalTitle}</Modal.Header>
      <Modal.Body>
        <div className={styles.modalBody}>
          <p>
            {kindText.what}
            <br />
            {kindText.template}
          </p>
          <Field label={n.label} error={failure.field} hint={n.hint}>
            {(control) => (
              <Input
                {...control}
                ref={nameInput}
                width={400}
                autoComplete="off"
                value={name}
                disabled={pending}
                onValueChange={(value) => {
                  setName(value);
                  setFailure((current) => ({ ...current, field: null }));
                }}
              />
            )}
          </Field>
          <p className={styles.muted}>{w.web.where(kindText.where)}</p>
          {failure.notice && <Notice kind="error">{failure.notice}</Notice>}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={pending} onClick={() => void submit()}>
            {w.web.create}
          </Button>
          <Button disabled={pending} onClick={onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}

/** Отказ создания из шаблона (таблицы §5.13.18, §5.13.24): название — под полем, остальное — в окне. */
export function templateFailure(error: unknown, config: CodeConfig | null): Failure {
  if (!(error instanceof ApiError)) {
    return { field: null, notice: errorText(error) };
  }
  const n = w.web.name;
  if (error.code === 'project_name_taken') {
    return { field: n.taken, notice: null };
  }
  const field = error.fields.find((item) => item.field === 'name' || item.field === 'kind');
  if (field?.field === 'kind') {
    return { field: null, notice: w.web.unknownKind };
  }
  const nameTexts: Record<string, string> = { required: n.enter, too_long: n.tooLong, invalid_format: n.enter };
  if (field && nameTexts[field.code]) {
    return { field: nameTexts[field.code] ?? null, notice: null };
  }
  if (error.code === 'project_limit_reached') {
    return { field: null, notice: w.limitReached(Number(error.details.max ?? config?.max_projects)) };
  }
  return { field: null, notice: errorText(error) };
}

/** Состояние, которое экран проекта получает при переходе после создания (§5.13.3, §5.13.4). */
export interface CreatedState {
  assistantRequest?: string | null;
  importReport?: CodeCreated['import'];
}

/** Состояние перехода на `/code` кнопкой «Новый проект»: фокус — на заголовок «Что нужно сделать» (§5.13.17). */
export interface HomeState {
  focusKinds?: boolean;
}

type HomeModal = 'example' | 'zip' | WebKind;

/** Начало раздела `/code` (концепция §5.13.17): что нужно сделать — пять видов кода. */
export function CodeHome() {
  const navigate = useNavigate();
  const location = useLocation();
  const { touchProject } = useCode();
  const [modal, setModal] = useState<HomeModal | null>(null);
  const rememberOpener = useFocusReturn(modal !== null);
  const kindsHeading = useRef<HTMLHeadingElement>(null);
  const openModal = (kind: HomeModal, opener: string) => {
    rememberOpener(opener);
    setModal(kind);
  };
  usePageTitle(w.title);

  const focusKinds = Boolean((location.state as HomeState | null)?.focusKinds);
  useEffect(() => {
    if (focusKinds) {
      kindsHeading.current?.focus();
      navigate(location.pathname, { replace: true, state: null });
    }
  }, [focusKinds, location.pathname, navigate]);

  const opened = (created: CodeCreated) => {
    touchProject(created.project);
    const state: CreatedState = { importReport: created.import };
    navigate(`/code/${created.project.id}`, { state });
  };

  return (
    <SectionLayout title={w.title}>
      <div className={styles.kindsColumn}>
        <p className={styles.intro}>{w.web.intro}</p>
        <h2 className={styles.kindsTitle} ref={kindsHeading} tabIndex={-1}>
          {w.web.chooseKind}
        </h2>
        <div className={styles.kinds}>
          {KINDS.map((kind) => {
            const text = kindTexts(kind);
            const opener = `[data-opener="kind-${kind}"] button`;
            return (
              <section key={kind} className={styles.kind} aria-labelledby={`kind-${kind}`}>
                <div className={styles.kindHead}>
                  <h3 id={`kind-${kind}`} className={styles.kindTitle}>
                    {text.title}
                  </h3>
                  <span data-opener={`kind-${kind}`}>
                    <Button
                      aria-label={text.actionLabel}
                      onClick={() => (kind === 'gptool' ? navigate('/code/new') : openModal(kind, opener))}
                    >
                      {text.action}
                    </Button>
                  </span>
                </div>
                <p className={styles.kindText}>{text.what}</p>
                <p className={styles.muted}>{w.web.where(text.where)}</p>
                {kind === 'gptool' && (
                  <p className={styles.muted}>
                    {w.web.alsoCan}{' '}
                    <span data-opener="example">
                      <Link component="button" onClick={() => openModal('example', '[data-opener="example"] button')}>
                        {w.web.fromExample}
                      </Link>
                    </span>
                    {' · '}
                    <span data-opener="zip">
                      <Link component="button" onClick={() => openModal('zip', '[data-opener="zip"] button')}>
                        {w.web.uploadZip}
                      </Link>
                    </span>
                  </p>
                )}
              </section>
            );
          })}
        </div>
        <p className={styles.muted}>{w.web.footer}</p>
      </div>
      {modal === 'example' && <ExampleModal onClose={() => setModal(null)} onCreated={opened} />}
      {modal === 'zip' && <ImportModal onClose={() => setModal(null)} onCreated={opened} />}
      {modal !== null && modal !== 'example' && modal !== 'zip' && (
        <TemplateModal kind={modal} onClose={() => setModal(null)} onCreated={opened} />
      )}
    </SectionLayout>
  );
}
