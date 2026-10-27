import { Button, Loader, SidePage, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useState } from 'react';

import { api, isApiError } from '../api/client';
import type { CodeProject, CodeSnapshot } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { Notice } from '../components/Notice';
import { texts } from '../texts';
import { formatDateTime } from './rules';
import styles from './VersionsPanel.module.css';

const v = texts.workshop.versions;

type Snapshots = { status: 'loading' } | { status: 'failed' } | { status: 'ready'; items: CodeSnapshot[] };

function reasonText(snapshot: CodeSnapshot): string {
  switch (snapshot.reason) {
    case 'edit_accepted':
      return v.reasons.edit_accepted(snapshot.accepted_files ?? 0);
    case 'build_succeeded':
      return v.reasons.build_succeeded;
    case 'restored':
      // `restored_from` — время версии-источника (контракт §7.5.6): по образцу «от 12.10.2026, 11:20».
      return v.reasons.restored(snapshot.restored_from ? formatDateTime(snapshot.restored_from) : '');
    default:
      return v.reasons.before_restore;
  }
}

interface Props {
  projectId: string;
  max: number;
  onClose: () => void;
  /** Файлы проекта до возврата сохранены: перед возвратом — несохранённое. */
  beforeRestore: () => Promise<boolean>;
  onRestored: (project: CodeProject) => void;
  /** Идёт возврат: редактор только для чтения. */
  onPending: (pending: boolean) => void;
  onProjectGone: () => void;
  sectionOff: (error: unknown) => boolean;
  /** Проект JS или HTML: сборки нет — тексты без неё (§5.13.17). */
  web?: boolean;
}

/** Версии проекта — `SidePage` 480 px (концепция §5.13.13). */
export function VersionsPanel({
  projectId,
  max,
  onClose,
  beforeRestore,
  onRestored,
  onPending,
  onProjectGone,
  sectionOff,
  web = false,
}: Props) {
  const [snapshots, setSnapshots] = useState<Snapshots>({ status: 'loading' });
  const [restoring, setRestoring] = useState<CodeSnapshot | null>(null);
  const [pending, setPending] = useState(false);

  const load = useCallback(() => {
    api.listCodeSnapshots(projectId).then(
      ({ items }) => setSnapshots({ status: 'ready', items }),
      (error: unknown) => {
        if (isApiError(error, 'not_found')) {
          onProjectGone();
        } else if (!sectionOff(error)) {
          setSnapshots({ status: 'failed' });
        }
      },
    );
  }, [onProjectGone, projectId, sectionOff]);

  useEffect(() => {
    load();
  }, [load]);

  const restore = async () => {
    if (!restoring) {
      return;
    }
    setPending(true);
    await beforeRestore();
    onPending(true);
    try {
      const project = await api.restoreCodeSnapshot(projectId, restoring.id);
      onRestored(project);
      SingleToast.push(v.restored);
      load();
    } catch (error) {
      if (isApiError(error, 'not_found')) {
        // Версию вытеснил предел — или удалён весь проект: проверка — тем же списком.
        SingleToast.push(v.gone(max));
        load();
      } else if (!sectionOff(error)) {
        SingleToast.push(texts.common.actionFailed, { use: 'error' });
      }
    } finally {
      onPending(false);
      setPending(false);
      setRestoring(null);
    }
  };

  let body: React.ReactNode;
  if (snapshots.status === 'loading') {
    body = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else if (snapshots.status === 'failed') {
    body = (
      <Notice kind="error" action={{ label: texts.common.retry, onClick: load }}>
        {v.failed}
      </Notice>
    );
  } else if (snapshots.items.length === 0) {
    body = <p className={styles.muted}>{web ? texts.workshop.web.versionsEmpty : v.empty}</p>;
  } else {
    body = (
      <ul className={styles.list}>
        {snapshots.items.map((snapshot) => {
          const date = formatDateTime(snapshot.created_at);
          return (
            <li key={snapshot.id} className={styles.row}>
              <span className={styles.time}>{date}</span>
              <span className={styles.reason}>{reasonText(snapshot)}</span>
              <Button size="small" aria-label={v.restoreLabel(date)} onClick={() => setRestoring(snapshot)}>
                {v.restore}
              </Button>
            </li>
          );
        })}
      </ul>
    );
  }

  return (
    <SidePage width={480} blockBackground ignoreOutsideClick onClose={onClose}>
      <SidePage.Header>{v.title}</SidePage.Header>
      <SidePage.Body>
        <SidePage.Container>
          <div className={styles.body}>
            <p className={styles.muted}>{web ? texts.workshop.web.versionsNote(max) : v.note(max)}</p>
            {body}
          </div>
        </SidePage.Container>
      </SidePage.Body>
      {restoring && (
        <ConfirmModal
          title={v.confirmTitle(formatDateTime(restoring.created_at))}
          body={v.confirmBody}
          action={v.confirm}
          pending={pending}
          onConfirm={() => void restore()}
          onCancel={() => setRestoring(null)}
        />
      )}
    </SidePage>
  );
}
