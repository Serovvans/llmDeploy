import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import {
  Button,
  Checkbox,
  FileUploader,
  Gapped,
  Link,
  MiniModal,
  Modal,
  Radio,
  RadioGroup,
  SingleToast,
  Spinner,
} from '@skbkontur/react-ui';
import { useEffect, useId, useRef, useState } from 'react';

import { api, isAbort, type KbUploadTarget } from '../../api/client';
import type { KbScope, PortalConfig } from '../../api/types';
import { useMediaUploads } from '../../chat/MediaUploads';
import {
  checkKbFile,
  isKbMedia,
  kbExtensions,
  kbLimitsHint,
  kbUploadFailure,
  uploadProgressText,
} from '../../kb/documents';
import { texts } from '../../texts';
import { ConfigNotice } from '../ConfigNotice';
import { Notice } from '../Notice';
import { ProgressBar } from '../ProgressBar';
import styles from './AddDocumentsModal.module.css';
import { TagsField, type TagsProblem } from './TagsField';

const t = texts.kb.upload;

interface PickedFile {
  key: string;
  file: File;
  /** Запись (аудио, видео): уходит своим маршрутом, строго по одной, с ходом загрузки. */
  media: boolean;
  /** `waiting` — отправлен по кнопке и ждёт своей очереди. */
  status: 'picked' | 'waiting' | 'uploading' | 'uploaded' | 'failed';
  /** Ход отправки записи, проценты. */
  percent: number;
  error: string | null;
  /** Файл не дошёл не по своей вине: уйдёт снова по «Добавить», запись — ещё и по «Повторить». */
  retry: boolean;
}

interface AddDocumentsModalProps {
  /** Пределы из настроек; `null` — настройки не загрузились, файлы проверяет только сервер. */
  config: PortalConfig | null;
  /** База открытой вкладки — предвыбранный вариант «Куда добавить». */
  scope: KbScope;
  /** Администратор: только ему предлагаются «Министерства». */
  admin: boolean;
  /** Отметка «Это документация CoGIS» стоит сразу (переход из помощника CoGIS). */
  cogis?: boolean;
  /** Документ загружен: список пора обновить. */
  onUploaded: () => void;
  onClose: () => void;
}

function isBusy(item: PickedFile): boolean {
  return item.status === 'waiting' || item.status === 'uploading';
}

/**
 * Добавление документов и записей (концепция §5.6): файлы уходят по кнопке «Добавить», по одному запросу
 * на файл; место и теги — одни на все файлы окна. Записи загружаются строго по одной, документы их не ждут.
 */
export function AddDocumentsModal(props: AddDocumentsModalProps) {
  const { config, admin, onUploaded, onClose } = props;
  const [files, setFiles] = useState<PickedFile[]>([]);
  // Сотруднику «Министерства» не предлагаются: окно, открытое с этой вкладки, предлагает общую базу.
  const [scope, setScope] = useState<KbScope>(props.scope === 'ministries' && !admin ? 'shared' : props.scope);
  // Место, выбранное сотрудником, запись уже не переставляет; переход из помощника CoGIS — тоже его выбор.
  const [scopeChosen, setScopeChosen] = useState(props.cogis ?? false);
  const [tags, setTags] = useState<string[]>([]);
  const [tagsProblem, setTagsProblem] = useState<TagsProblem | null>(null);
  const [isCogis, setIsCogis] = useState(props.cogis ?? false);
  const [noFiles, setNoFiles] = useState(false);
  const [confirmingAbort, setConfirmingAbort] = useState(false);
  // Очередь записей — общая с чатом: пока запись загружается там, запись окна ждёт.
  const mediaUploads = useMediaUploads();
  const queued = useRef(new Set<string>());
  // Свой на каждое открытие окна: создаётся эффектом ниже и прерывается вместе с окном.
  const documentUploads = useRef(new AbortController());
  const uploader = useRef<FileUploader>(null);
  const counter = useRef(0);
  const uploadedInRun = useRef(0);
  const wasBusy = useRef(false);
  const noFilesId = useId();

  const busy = files.some(isBusy);
  const someUploaded = files.some((item) => item.status === 'uploaded');
  const hasRecording = files.some((item) => item.media);
  const hasDocument = files.some((item) => !item.media);

  // Загрузка закончилась: число добавленных — в уведомлении; окно закрывается, если загружены все файлы.
  useEffect(() => {
    if (busy) {
      wasBusy.current = true;
      return;
    }
    if (!wasBusy.current) {
      return;
    }
    wasBusy.current = false;
    const uploaded = uploadedInRun.current;
    uploadedInRun.current = 0;
    if (uploaded > 0) {
      // Уведомление библиотеки само перерисовывает страницу: из эффекта его вызывают отложенно.
      queueMicrotask(() => SingleToast.push(t.added(uploaded)));
    }
    if (files.length > 0 && files.every((item) => item.status === 'uploaded')) {
      onClose();
    }
  }, [busy, files, onClose]);

  // Окно закрыто любым способом (в том числе кнопкой «Назад» браузера): незагруженное не загружается.
  useEffect(() => {
    const controller = new AbortController();
    const keys = queued.current;
    documentUploads.current = controller;
    return () => {
      keys.forEach((key) => mediaUploads.cancel(key));
      controller.abort();
    };
  }, [mediaUploads]);

  // Пока идёт загрузка, браузер переспрашивает о закрытии вкладки (концепция §5.6).
  useEffect(() => {
    if (!busy) {
      return;
    }
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [busy]);

  const pick = (picked: File[]) => {
    setNoFiles(false);
    const added = picked.map((file): PickedFile => {
      counter.current += 1;
      const error = config && checkKbFile(file, config);
      return {
        key: `kb-file-${counter.current}`,
        file,
        media: isKbMedia(file, config),
        status: error ? 'failed' : 'picked',
        percent: 0,
        error,
        retry: false,
      };
    });
    setFiles((current) => [...current, ...added]);
    // Записи совещаний по умолчанию — в личную базу; выбор, сделанный вручную, не переставляется.
    if (!scopeChosen && added.some((item) => item.media)) {
      setScope('personal');
    }
    // Список файлов окно ведёт само: у каждого свой ход и своя ошибка.
    uploader.current?.reset();
  };

  const patch = (key: string, change: Partial<PickedFile>) =>
    setFiles((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)));

  /** Убирает файл из списка; запись в очереди из неё выходит, идущая загрузка записи отменяется. */
  const remove = (key: string) => {
    mediaUploads.cancel(key);
    setFiles((current) => current.filter((item) => item.key !== key));
  };

  /** Загружает один файл; отменённая загрузка следа не оставляет. */
  const upload = async (item: PickedFile, send: () => Promise<unknown>) => {
    patch(item.key, { status: 'uploading' });
    try {
      await send();
      uploadedInRun.current += 1;
      patch(item.key, { status: 'uploaded' });
      onUploaded();
    } catch (error) {
      if (!isAbort(error)) {
        const failure = kbUploadFailure(error, { config, media: item.media, withTags: tags.length > 0 });
        patch(item.key, { status: 'failed', error: failure.text, retry: failure.retry });
      }
    }
  };

  const start = (items: PickedFile[]) => {
    const keys = new Set(items.map((item) => item.key));
    const target: KbUploadTarget = { scope, tags };
    setFiles((current) =>
      current.map((item) => (keys.has(item.key) ? { ...item, status: 'waiting', percent: 0, error: null, retry: false } : item)),
    );
    for (const item of items.filter((candidate) => candidate.media)) {
      queued.current.add(item.key);
      mediaUploads.enqueue(item.key, (signal) =>
        upload(item, () => api.uploadKbMedia(item.file, target, (percent) => patch(item.key, { percent }), signal)),
      );
    }
    const documents = items.filter((candidate) => !candidate.media);
    const { signal } = documentUploads.current;
    void (async () => {
      for (const item of documents) {
        if (signal.aborted) {
          return;
        }
        await upload(item, () => api.uploadKbDocument(item.file, { ...target, isCogis: scope === 'shared' && isCogis }, signal));
      }
    })();
  };

  const submit = () => {
    // Уходят выбранные файлы и те, чья загрузка оборвалась; отклонённые остаются с ошибкой.
    const pending = files.filter((item) => item.status === 'picked' || item.retry);
    if (pending.length === 0 && !someUploaded) {
      setNoFiles(true);
      return;
    }
    start(pending);
  };

  const requestClose = () => (busy ? setConfirmingAbort(true) : onClose());

  /** Загрузки прерывает само закрытие окна — тем же путём, что и любое другое его исчезновение. */
  const abort = onClose;

  return (
    <Modal width={560} ignoreBackgroundClick onClose={requestClose}>
      <Modal.Header>{t.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.stack}>
          <div>
            <FileUploader
              ref={uploader}
              multiple
              hideFiles
              width="100%"
              accept={config ? kbExtensions(config).join(',') : undefined}
              uploaderText={t.pick}
              disabled={busy}
              error={noFiles}
              aria-describedby={noFiles ? noFilesId : undefined}
              onAttach={(attached) => pick(attached.map((item) => item.originalFile))}
            />
            {noFiles && (
              <p id={noFilesId} className={styles.error} role="alert">
                {t.noFiles}
              </p>
            )}
            {config ? (
              <p className={styles.hint}>{kbLimitsHint(config)}</p>
            ) : (
              <div className={styles.hint}>
                <ConfigNotice />
              </div>
            )}
          </div>
          {files.length > 0 && (
            <ul className={styles.files}>
              {files.map((item) => (
                <li key={item.key} className={styles.file}>
                  <span className={styles.fileLine}>
                    <span className={styles.fileName}>{item.file.name}</span>
                    {isBusy(item) && !item.media && <Spinner type="mini" caption={null} />}
                    {item.status === 'waiting' && item.media && (
                      <>
                        <span className={styles.muted}>{t.waiting}</span>
                        <Link component="button" onClick={() => remove(item.key)}>
                          {t.removeWaiting}
                        </Link>
                      </>
                    )}
                    {item.status === 'uploading' && item.media && (
                      <>
                        <span className={styles.muted}>{uploadProgressText(item.file, item.percent)}</span>
                        <Link component="button" onClick={() => remove(item.key)}>
                          {t.cancel}
                        </Link>
                      </>
                    )}
                    {item.status === 'uploaded' && (
                      <span className={styles.muted}>
                        <IconCheckARegular16 aria-hidden="true" /> {t.uploaded}
                      </span>
                    )}
                    {!isBusy(item) && item.status !== 'uploaded' && (
                      <button
                        type="button"
                        className={styles.remove}
                        aria-label={texts.chat.files.remove(item.file.name)}
                        onClick={() => remove(item.key)}
                      >
                        <IconXRegular16 aria-hidden="true" />
                      </button>
                    )}
                  </span>
                  {item.status === 'uploading' && item.media && (
                    <ProgressBar percent={item.percent} label={t.progressLabel(item.file.name)} />
                  )}
                  {item.error && (
                    <span className={styles.error}>
                      <span role="alert">{item.error}</span>
                      {item.media && item.retry && (
                        <>
                          {' '}
                          <Link component="button" onClick={() => start([item])}>
                            {texts.common.retry}
                          </Link>
                        </>
                      )}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          {hasRecording && <Notice kind="info">{t.recordingNotice}</Notice>}
          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>{t.where}</legend>
            <RadioGroup<KbScope>
              value={scope}
              disabled={busy}
              onValueChange={(next) => {
                setScope(next);
                setScopeChosen(true);
              }}
            >
              <Gapped vertical gap={8}>
                <Radio<KbScope> value="shared">{t.toShared}</Radio>
                <Radio<KbScope> value="personal">{t.toPersonal}</Radio>
                {admin && <Radio<KbScope> value="ministries">{t.toMinistries}</Radio>}
              </Gapped>
            </RadioGroup>
          </fieldset>
          <TagsField
            tags={tags}
            limits={config?.kb.tags ?? null}
            problem={tagsProblem}
            hint={t.tagsHint}
            disabled={busy}
            onChange={(next, problem) => {
              setTags(next);
              setTagsProblem(problem);
            }}
          />
          {/* К записям отметка не применяется: если выбраны одни записи, её нет вовсе. */}
          {scope === 'shared' && (hasDocument || !hasRecording) && (
            <div>
              <Checkbox checked={isCogis} onValueChange={setIsCogis} disabled={busy}>
                {t.cogis}
              </Checkbox>
              <p className={styles.checkboxHint}>{config?.code.enabled ? t.cogisHintCode : t.cogisHint}</p>
              {hasRecording && <p className={styles.checkboxHint}>{t.cogisNotForRecordings}</p>}
            </div>
          )}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          {/* Пока тег в поле не исправлен, ни один файл не отправляется. */}
          <Button use="primary" loading={busy} disabled={tagsProblem?.blocks} onClick={submit}>
            {t.submit}
          </Button>
          <Button onClick={requestClose}>{someUploaded ? t.close : texts.common.cancel}</Button>
        </Gapped>
      </Modal.Footer>
      {confirmingAbort && (
        <MiniModal onClose={() => setConfirmingAbort(false)}>
          <MiniModal.Header>{t.abort.title}</MiniModal.Header>
          <MiniModal.Body>{t.abort.body}</MiniModal.Body>
          <MiniModal.Footer>
            <Button use="danger" onClick={abort}>
              {t.abort.action}
            </Button>
            <Button autoFocus onClick={() => setConfirmingAbort(false)}>
              {t.abort.stay}
            </Button>
          </MiniModal.Footer>
        </MiniModal>
      )}
    </Modal>
  );
}
