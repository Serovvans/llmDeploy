import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Button, Checkbox, FileUploader, Gapped, Modal, Radio, RadioGroup, SingleToast, Spinner } from '@skbkontur/react-ui';
import { useId, useRef, useState } from 'react';

import { api, NetworkError } from '../../api/client';
import type { KbScope, PortalConfig } from '../../api/types';
import { checkKbFile, kbUploadErrorText, megabytes } from '../../kb/documents';
import { texts } from '../../texts';
import styles from './AddDocumentsModal.module.css';

const t = texts.kb.upload;

interface PickedFile {
  key: string;
  file: File;
  status: 'picked' | 'uploading' | 'uploaded' | 'failed';
  error: string | null;
  /** Загрузка оборвалась: файл уйдёт снова по кнопке «Добавить». */
  retry: boolean;
}

interface AddDocumentsModalProps {
  limits: PortalConfig['kb'];
  /** База открытой вкладки — предвыбранный вариант «Куда добавить». */
  scope: KbScope;
  /** Отметка «Это документация CoGIS» стоит сразу (переход из помощника CoGIS). */
  cogis?: boolean;
  /** Документ загружен: список пора обновить. */
  onUploaded: () => void;
  onClose: () => void;
}

/** Добавление документов (концепция §5.6): файлы уходят по одному запросу по кнопке «Добавить». */
export function AddDocumentsModal(props: AddDocumentsModalProps) {
  const { limits, scope: initialScope, onUploaded, onClose } = props;
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [scope, setScope] = useState<KbScope>(initialScope);
  const [isCogis, setIsCogis] = useState(props.cogis ?? false);
  const [noFiles, setNoFiles] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const uploader = useRef<FileUploader>(null);
  const counter = useRef(0);
  const noFilesId = useId();
  const someUploaded = files.some((item) => item.status === 'uploaded');

  const pick = (picked: File[]) => {
    setNoFiles(false);
    setFiles((current) => [
      ...current,
      ...picked.map((file) => {
        counter.current += 1;
        const error = checkKbFile(file, limits);
        return {
          key: `file-${counter.current}`,
          file,
          status: error ? ('failed' as const) : ('picked' as const),
          error,
          retry: false,
        };
      }),
    ]);
    // Список файлов окно ведёт само: у каждого свой ход и своя ошибка.
    uploader.current?.reset();
  };

  const patch = (key: string, change: Partial<PickedFile>) =>
    setFiles((current) => current.map((item) => (item.key === key ? { ...item, ...change } : item)));

  const submit = async () => {
    // Уходят выбранные файлы и те, чья загрузка оборвалась; отклонённые остаются с ошибкой.
    const pending = files.filter((item) => item.status === 'picked' || item.retry);
    if (pending.length === 0 && !someUploaded) {
      setNoFiles(true);
      return;
    }
    setSubmitting(true);
    let uploaded = 0;
    for (const item of pending) {
      patch(item.key, { status: 'uploading', error: null, retry: false });
      try {
        await api.uploadKbDocument(item.file, scope, scope === 'shared' && isCogis);
        patch(item.key, { status: 'uploaded' });
        uploaded += 1;
        onUploaded();
      } catch (error) {
        patch(item.key, {
          status: 'failed',
          error: kbUploadErrorText(error, limits),
          retry: error instanceof NetworkError,
        });
      }
    }
    setSubmitting(false);
    if (uploaded > 0) {
      SingleToast.push(t.added(uploaded));
    }
    // Все файлы загружены — окно закрывается; иначе остаётся с ошибками у отклонённых.
    const rejectedBefore = files.length - pending.length - files.filter((item) => item.status === 'uploaded').length;
    if (uploaded === pending.length && rejectedBefore === 0) {
      onClose();
    }
  };

  return (
    <Modal width={560} onClose={onClose} disableClose={submitting}>
      <Modal.Header>{t.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.stack}>
          <div>
            <FileUploader
              ref={uploader}
              multiple
              hideFiles
              width="100%"
              accept={limits.document_extensions.join(',')}
              uploaderText={t.pick}
              disabled={submitting}
              error={noFiles}
              aria-describedby={noFiles ? noFilesId : undefined}
              onAttach={(attached) => pick(attached.map((item) => item.originalFile))}
            />
            {noFiles && (
              <p id={noFilesId} className={styles.error} role="alert">
                {t.noFiles}
              </p>
            )}
            <p className={styles.hint}>{t.limits(megabytes(limits.document_max_bytes), limits.document_max_pages)}</p>
          </div>
          {files.length > 0 && (
            <ul className={styles.files}>
              {files.map((item) => (
                <li key={item.key} className={styles.file}>
                  <span className={styles.fileLine}>
                    <span className={styles.fileName}>{item.file.name}</span>
                    {item.status === 'uploading' && <Spinner type="mini" caption={null} />}
                    {item.status === 'uploaded' && (
                      <span className={styles.uploaded}>
                        <IconCheckARegular16 aria-hidden="true" /> {t.uploaded}
                      </span>
                    )}
                    {item.status !== 'uploading' && item.status !== 'uploaded' && (
                      <button
                        type="button"
                        className={styles.remove}
                        aria-label={texts.chat.files.remove(item.file.name)}
                        disabled={submitting}
                        onClick={() => setFiles((current) => current.filter((other) => other.key !== item.key))}
                      >
                        <IconXRegular16 aria-hidden="true" />
                      </button>
                    )}
                  </span>
                  {item.error && (
                    <span className={styles.error} role="alert">
                      {item.error}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
          <fieldset className={styles.fieldset}>
            <legend className={styles.legend}>{t.where}</legend>
            <RadioGroup<KbScope> value={scope} onValueChange={setScope} disabled={submitting}>
              <Gapped vertical gap={8}>
                <Radio<KbScope> value="shared">{t.toShared}</Radio>
                <Radio<KbScope> value="personal">{t.toPersonal}</Radio>
              </Gapped>
            </RadioGroup>
          </fieldset>
          {scope === 'shared' && (
            <div>
              <Checkbox checked={isCogis} onValueChange={setIsCogis} disabled={submitting}>
                {t.cogis}
              </Checkbox>
              <p className={styles.checkboxHint}>{t.cogisHint}</p>
            </div>
          )}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={submitting} onClick={() => void submit()}>
            {t.submit}
          </Button>
          <Button disabled={submitting} onClick={onClose}>
            {someUploaded ? t.close : texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
