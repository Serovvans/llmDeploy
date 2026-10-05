import { Hint, Input, Kebab, Loader, MenuItem, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import type { Dialog } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import { attachHint } from '../chat/files';
import { EMPTY_DRAFT, NEW_CHAT } from '../chat/state';
import { Composer } from '../components/chat/Composer';
import { HistoryPanel } from '../components/chat/HistoryPanel';
import { MessageList } from '../components/chat/MessageList';
import { ConfirmModal } from '../components/ConfirmModal';
import { EmptyState } from '../components/EmptyState';
import { Notice } from '../components/Notice';
import { PageHeader } from '../components/PageHeader';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import styles from './ChatScreen.module.css';

const t = texts.chat;
const TITLE_MAX = 200;
const POLL_MS = 5000;

interface TitleEditorProps {
  initial: string;
  onSave: (title: string) => Promise<void>;
  onDone: () => void;
}

/** Переименование на месте: Enter сохраняет, Esc отменяет, потеря фокуса сохраняет (концепция §5.5). */
function TitleEditor({ initial, onSave, onDone }: TitleEditorProps) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const finished = useRef(false);

  const save = async (keepOpenOnError: boolean) => {
    if (finished.current) {
      return;
    }
    const title = value.trim();
    const invalid = !title ? t.enterTitle : title.length > TITLE_MAX ? t.titleTooLong : null;
    if (invalid) {
      // Неверное название не сохраняется: по Enter — подсказка, при потере фокуса — отмена.
      if (keepOpenOnError) {
        setError(invalid);
      } else {
        onDone();
      }
      return;
    }
    finished.current = true;
    if (title !== initial) {
      try {
        await onSave(title);
      } catch (failure) {
        SingleToast.push(errorText(failure), { use: 'error' });
      }
    }
    onDone();
  };

  return (
    <header className={styles.editor}>
      <h1 className="p-visually-hidden">{initial}</h1>
      <Input
        width={400}
        autoFocus
        selectAllOnFocus
        aria-label={t.titleLabel}
        error={error !== null}
        value={value}
        onValueChange={(next) => {
          setValue(next);
          setError(null);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            void save(true);
          } else if (event.key === 'Escape') {
            finished.current = true;
            onDone();
          }
        }}
        onBlur={() => void save(false)}
      />
      {error && (
        <span className={styles.editorError} role="alert">
          {error}
        </span>
      )}
    </header>
  );
}

export function ChatScreen() {
  const { id } = useParams();
  const navigate = useNavigate();
  const chat = useChat();
  const { config } = useSession();
  const { openDialog, refreshDialog } = chat;
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Dialog | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const rememberOpener = useFocusReturn(removing !== null || renamingId !== null);

  const key = id ?? NEW_CHAT;
  const view = id ? chat.state.dialogs[id] : undefined;
  const dialog = id ? chat.state.history.items.find((item) => item.id === id) : undefined;
  const title = dialog?.title ?? t.newChat;
  const last = view?.messages.at(-1);
  const formingElsewhere = last?.status === 'streaming' && !view?.generation;
  usePageTitle(title);

  useEffect(() => {
    if (id) {
      openDialog(id);
    }
  }, [id, openDialog]);

  // Чат удалён (здесь или в другой вкладке): открывается пустой новый чат.
  const deleted = view?.status === 'deleted';
  useEffect(() => {
    if (deleted) {
      navigate('/chat', { replace: true });
    }
  }, [deleted, navigate]);

  // Ответ формируется в другой вкладке: сообщения перезапрашиваются, пока состояние не сменится.
  useEffect(() => {
    if (!id || !formingElsewhere) {
      return;
    }
    const timer = window.setInterval(() => refreshDialog(id), POLL_MS);
    return () => window.clearInterval(timer);
  }, [id, formingElsewhere, refreshDialog]);

  const newChat = () => navigate('/chat');
  const onCreated = (createdId: string) => navigate(`/chat/${createdId}`, { replace: true });

  const startRename = (target: Dialog, opener: string) => {
    rememberOpener(`[data-opener="${opener}"] [tabindex="0"]`);
    if (target.id !== id) {
      navigate(`/chat/${target.id}`);
    }
    setRenamingId(target.id);
  };

  const startRemove = (target: Dialog, opener: string) => {
    rememberOpener(`[data-opener="${opener}"] [tabindex="0"]`);
    setRemoving(target);
  };

  const confirmRemove = async () => {
    if (!removing) {
      return;
    }
    setRemovePending(true);
    try {
      await chat.remove(removing.id);
      SingleToast.push(t.remove.done);
    } catch {
      // Чат остаётся в истории.
      SingleToast.push(t.remove.failed, { use: 'error' });
    } finally {
      setRemovePending(false);
      setRemoving(null);
    }
  };

  let body: React.ReactNode;
  if (view?.status === 'not_found') {
    body = (
      <EmptyState
        title={t.notFound.title}
        text={t.notFound.text}
        action={{ label: t.newChat, onClick: newChat }}
      />
    );
  } else if (view?.status === 'failed') {
    body = (
      <div className={styles.failed}>
        <Notice kind="error" action={{ label: texts.common.retry, onClick: () => id && refreshDialog(id) }}>
          {texts.common.actionFailed}
        </Notice>
      </div>
    );
  } else if (id && (!view || view.status === 'loading' || view.status === 'deleted')) {
    body = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else if (!id || !view || view.messages.length === 0) {
    body = <EmptyState title={t.empty.title} text={t.empty.text} />;
  } else {
    body = (
      <MessageList
        dialogId={id}
        view={view}
        onRegenerate={() => chat.regenerate(id)}
        onLoadOlder={() => chat.loadOlder(id)}
      />
    );
  }

  const usable = view?.status !== 'not_found' && view?.status !== 'failed';
  const lastAnswer = last?.role === 'assistant' ? last : undefined;

  return (
    <div className={styles.screen}>
      <HistoryPanel
        history={chat.state.history}
        onNewChat={newChat}
        onRename={(target) => startRename(target, `history-${target.id}`)}
        onRemove={(target) => startRemove(target, `history-${target.id}`)}
        onShowMore={() => chat.loadHistory(true)}
        onRetry={() => chat.loadHistory(false)}
      />
      <div className={styles.area}>
        {dialog && renamingId === dialog.id ? (
          <TitleEditor
            key={dialog.id}
            initial={title}
            onSave={(next) => chat.rename(dialog.id, next)}
            onDone={() => setRenamingId(null)}
          />
        ) : (
          <PageHeader title={title}>
            {dialog && (
              <span data-opener="header">
                {/* Подсказка — слева: сверху ей нет места, а снизу она закрыла бы открытое меню. */}
                <Hint text={texts.users.actions} pos="left">
                  <Kebab aria-label={`${texts.users.actions}: ${title}`}>
                    <MenuItem onClick={() => startRename(dialog, 'header')}>{t.menu.rename}</MenuItem>
                    <MenuItem onClick={() => startRemove(dialog, 'header')}>{t.menu.remove}</MenuItem>
                  </Kebab>
                </Hint>
              </span>
            )}
          </PageHeader>
        )}
        <div className={styles.body}>{body}</div>
        {usable && (
          <>
            {lastAnswer && lastAnswer.dropped_messages > 0 && (
              <div className={styles.longDialog}>
                <Notice kind="info" action={{ label: t.newChat, onClick: newChat }}>
                  {t.longDialog}
                </Notice>
              </div>
            )}
            <Composer
              dialogId={id ?? null}
              draft={chat.state.drafts[key] ?? EMPTY_DRAFT}
              mode={chat.mode}
              answer={view?.generation ? 'generating' : formingElsewhere ? 'waiting' : 'idle'}
              attach={config ? { hint: attachHint(config.chat), extensions: config.chat.attachment_extensions } : null}
              onTextChange={(text) => chat.setText(key, text)}
              onModeChange={chat.setMode}
              onSend={() => chat.send(key, onCreated)}
              onStop={() => id && chat.stop(id)}
              onNotice={(notice) => chat.setNotice(key, notice)}
              onAddFiles={(files) => chat.addFiles(key, files, onCreated)}
              onRetryFile={(fileKey) => chat.retryFile(key, fileKey)}
              onRemoveFile={(fileKey) => chat.removeFile(key, fileKey)}
            />
          </>
        )}
      </div>
      {removing && (
        <ConfirmModal
          title={t.remove.title(removing.title ?? t.newChat)}
          body={t.remove.body}
          action={t.remove.action}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </div>
  );
}
