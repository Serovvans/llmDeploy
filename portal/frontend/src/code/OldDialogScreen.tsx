import { Hint, Kebab, Loader, MenuItem, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import type { Source } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { exportDocx } from '../chat/exportDocx';
import { MessageList } from '../components/chat/MessageList';
import { ConfirmModal } from '../components/ConfirmModal';
import { EmptyState } from '../components/EmptyState';
import { useDocumentViewer } from '../components/kb/DocumentViewer';
import { Notice } from '../components/Notice';
import { usePageTitle } from '../hooks/usePageTitle';
import { useMemoryUi } from '../memory/useMemoryUi';
import { texts } from '../texts';
import styles from './OldDialogScreen.module.css';
import { SectionLayout } from './SectionLayout';

const w = texts.workshop;
const labels = texts.dialogs.cogis;

/** Прежний диалог помощника CoGIS `/code/dialogs/:id` — только для чтения (концепция §5.13.14). */
export function OldDialogScreen() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const cogis = useDialogs('cogis');
  const { openDialog } = cogis;
  const view = cogis.state.dialogs[id];
  const dialog = cogis.history.items.find((item) => item.id === id);
  // Until the dialog is known its title is unknown too: no header instead of a placeholder name.
  const title = dialog || view?.status === 'ready' ? (dialog?.title ?? labels.newLabel) : undefined;
  const [removing, setRemoving] = useState(false);
  const [pending, setPending] = useState(false);
  const viewer = useDocumentViewer();
  const { open: openViewer, close: closeViewer } = viewer;
  // Модель этот диалог больше не получает: кнопки, панели и индикатора памяти нет — только перенос.
  const memory = useMemoryUi({
    kind: 'cogis',
    dialogId: null,
    title: dialog?.title ?? null,
    view: undefined,
    available: false,
    params: { action: 'write' },
    busy: false,
  });
  usePageTitle(view?.status === 'not_found' ? w.old.notFound.title : (title ?? w.title));

  useEffect(() => {
    openDialog(id);
  }, [id, openDialog]);

  useEffect(() => closeViewer, [id, closeViewer]);

  const deleted = view?.status === 'deleted';
  useEffect(() => {
    if (deleted) {
      navigate('/code', { replace: true });
    }
  }, [deleted, navigate]);

  const openSource = useCallback(
    (source: Source, openerId: string) =>
      openViewer(
        {
          documentId: source.document_id,
          title: source.document_title,
          fragmentId: source.fragment_id,
          page: source.page ?? undefined,
          timecode: source.timecode_seconds ?? undefined,
        },
        `[data-opener="${openerId}"]`,
      ),
    [openViewer],
  );

  const confirmRemove = async () => {
    setPending(true);
    try {
      await cogis.remove(id);
      SingleToast.push(labels.removed);
      navigate('/code', { replace: true });
    } catch {
      SingleToast.push(labels.removeFailed, { use: 'error' });
    } finally {
      setPending(false);
      setRemoving(false);
    }
  };

  const transfer = dialog && memory.transfer ? () => memory.transfer?.(dialog, '[data-opener="old-header"] [tabindex="0"]') : null;

  if (view?.status === 'not_found') {
    return (
      <SectionLayout bare>
        <EmptyState headingLevel="h1" title={w.old.notFound.title} text={w.old.notFound.text} action={{ label: w.allProjects, onClick: () => navigate('/code') }} />
      </SectionLayout>
    );
  }

  let body: React.ReactNode;
  if (view?.status === 'failed') {
    body = (
      <div className={styles.failed}>
        <Notice kind="error" action={{ label: texts.common.retry, onClick: () => cogis.refreshDialog(id) }}>
          {texts.common.actionFailed}
        </Notice>
      </div>
    );
  } else if (!view || view.status !== 'ready') {
    body = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else {
    body = (
      <MessageList
        dialogId={id}
        view={view}
        onLoadOlder={() => cogis.loadOlder(id)}
        onOpenSource={openSource}
        openingFragment={viewer.opening?.fragmentId ?? null}
        nothingFoundText={texts.cogis.nothingFound}
        memory={memory.feed}
      />
    );
  }

  return (
    <SectionLayout
      title={title}
      bare
      actions={
        dialog && (
          <span data-opener="old-header">
            <Hint text={texts.users.actions} pos="left">
              <Kebab aria-label={`${texts.users.actions}: ${title}`}>
                <MenuItem onClick={() => void exportDocx(id)}>{texts.chat.menu.exportDocx}</MenuItem>
                {transfer && <MenuItem onClick={transfer}>{texts.memory.transfer.menu}</MenuItem>}
                <MenuItem onClick={() => setRemoving(true)}>{texts.chat.menu.remove}</MenuItem>
              </Kebab>
            </Hint>
          </span>
        )
      }
    >
      <div className={styles.screen}>
        <div className={styles.body}>{body}</div>
        <div className={styles.notice}>
          <Notice kind="info" action={transfer ? { label: w.old.transfer, onClick: transfer } : undefined}>
            {transfer ? w.old.notice : w.old.noticeShort}
          </Notice>
        </div>
      </div>
      {viewer.element}
      {memory.dialogs}
      {removing && title !== undefined && (
        <ConfirmModal
          title={labels.removeTitle(title)}
          body={labels.removeBody}
          action={texts.chat.remove.action}
          pending={pending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(false)}
        />
      )}
    </SectionLayout>
  );
}
