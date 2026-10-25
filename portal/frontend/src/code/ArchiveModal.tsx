import { IconNetDownloadRegular16 } from '@skbkontur/icons/IconNetDownloadRegular16';
import { Button, Gapped, Link, Modal } from '@skbkontur/react-ui';
import { useState } from 'react';

import { api, isApiError } from '../api/client';
import type { CodeBuild } from '../api/types';
import { Notice } from '../components/Notice';
import { StepList } from '../components/StepList';
import { useCopy } from '../hooks/useCopy';
import { errorText, texts } from '../texts';
import styles from './ArchiveModal.module.css';
import { saveBlob } from './download';

const a = texts.workshop.archive;

function CopyValue({ label, value }: { label: string; value: string }) {
  const { label: copyLabel, copy } = useCopy();
  return (
    <div className={styles.value}>
      <span className={styles.valueLabel}>{label}</span>
      <code className={styles.mono}>{value}</code>
      <Link component="button" onClick={() => void copy(value)}>{copyLabel}</Link>
    </div>
  );
}

interface Props {
  projectId: string;
  projectName: string;
  build: CodeBuild;
  onClose: () => void;
  /** Архив недоступен: строка «Путь до CoGIS» перечитывается. */
  onRefused: () => void;
  sectionOff: (error: unknown) => boolean;
}

/** «Скачать для CoGIS» — окно с памяткой (концепция §5.13.9): окно и памятка в архиве говорят одно. */
export function ArchiveModal({ projectId, projectName, build, onClose, onRefused, sectionOff }: Props) {
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const files = build.archive?.files ?? [];
  const tool = files.find((file) => file.kind === 'tool')?.file_name ?? `${projectName}.dll`;
  const dllPath = `${a.pluginsDir}${tool}`;

  const download = async () => {
    setPending(true);
    setFailure(null);
    try {
      const { blob, fileName } = await api.downloadCodeArchive(projectId);
      saveBlob(blob, fileName ?? `${projectName}.zip`);
    } catch (error) {
      if (sectionOff(error)) {
        onClose();
      } else if (isApiError(error, 'archive_unavailable')) {
        setFailure(error.details.reason === 'outdated' ? a.outdated : a.notReady);
        onRefused();
      } else if (!isApiError(error, 'unauthenticated')) {
        setFailure(errorText(error));
      }
    } finally {
      setPending(false);
    }
  };

  const steps = [
    { title: a.step1, children: null },
    { title: a.step2, children: null },
    {
      children: (
        <>
          <div className={styles.values}>
            <CopyValue label={a.dllPath} value={dllPath} />
            {build.factory && <CopyValue label={a.factory} value={build.factory} />}
          </div>
          <p className={styles.stepText}>{a.step3}</p>
        </>
      ),
    },
    {
      title: a.step4,
      children: (
        <ul className={styles.tools}>
          {(build.tools ?? []).map((item) => (
            <li key={item.name}>{a.step4Tool(item.category, item.title)}</li>
          ))}
        </ul>
      ),
    },
  ];

  return (
    <Modal width={640} ignoreBackgroundClick onClose={onClose}>
      <Modal.Header>{a.title}</Modal.Header>
      <Modal.Body>
        <div className={styles.body}>
          <Notice kind="warning">{a.warning}</Notice>
          <section>
            <h2 className={styles.heading}>{a.contents}</h2>
            <ul className={styles.files}>
              {files.map((file) => (
                <li key={file.file_name}>
                  <span className={styles.mono}>{file.file_name}</span> — {a.kinds[file.kind]}
                </li>
              ))}
            </ul>
          </section>
          <section className={styles.steps}>
            <h2 className={styles.heading}>{a.howTo}</h2>
            <StepList steps={steps} />
          </section>
          <p className={styles.muted}>{a.footer}</p>
          {failure && <Notice kind="error">{failure}</Notice>}
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" icon={<IconNetDownloadRegular16 />} loading={pending} onClick={() => void download()}>
            {a.download}
          </Button>
          <Button onClick={onClose}>{a.close}</Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
