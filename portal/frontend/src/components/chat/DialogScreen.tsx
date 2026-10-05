import { Hint, Input, Kebab, Loader, MenuItem, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import type { Dialog, QuestionParams, Source } from '../../api/types';
import { useDialogs } from '../../chat/ChatProvider';
import { exportDocx } from '../../chat/exportDocx';
import { attachHint } from '../../chat/files';
import { EMPTY_DRAFT } from '../../chat/state';
import { useFocusReturn } from '../../hooks/useFocusReturn';
import { usePageTitle } from '../../hooks/usePageTitle';
import { useSession } from '../../session/SessionContext';
import { errorText, texts } from '../../texts';
import { ConfirmModal } from '../ConfirmModal';
import { EmptyState } from '../EmptyState';
import { useDocumentViewer } from '../kb/DocumentViewer';
import { Notice } from '../Notice';
import { PageHeader } from '../PageHeader';
import { Composer } from './Composer';
import styles from './DialogScreen.module.css';
import { HistoryPanel } from './HistoryPanel';
import { MessageList } from './MessageList';

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

type OnRefused = (error: unknown) => boolean;

interface DialogScreenProps {
  kind: 'chat' | 'sql' | 'cogis';
  /** Адрес раздела: `/chat`, `/sql`, `/cogis`. */
  basePath: string;
  /** Заголовок, пока диалог не открыт; у чата — «Новый чат». */
  sectionTitle: string;
  empty: { title: string; text: string };
  /** Строка действий и настроек над лентой (концепция §1.2). */
  toolbar?: React.ReactNode;
  /** Постоянное предупреждение над лентой. */
  banner?: React.ReactNode;
  /** Параметры вопроса своего вида диалога — на момент отправки. */
  params: QuestionParams;
  placeholder?: string;
  mono?: boolean;
  note?: string;
  /** Строка под ответом, когда поиск ничего не нашёл; по умолчанию — как в чате. */
  nothingFoundText?: string;
  onSendRefused?: OnRefused;
  /** Своя повторная генерация (SQL: с заменой удалённой схемы); по умолчанию — обычная. */
  onRegenerate?: (id: string) => void;
}

/**
 * Рабочий экран с историей, лентой и панелью запроса — общий для чата и инструментов (концепция §1.2):
 * меняются действия над лентой и подсказка в поле ввода.
 */
export function DialogScreen(props: DialogScreenProps) {
  const { kind, basePath } = props;
  const labels = texts.dialogs[kind];
  const { id } = useParams();
  const navigate = useNavigate();
  const chat = useDialogs(kind);
  const { config } = useSession();
  const { openDialog, refreshDialog, history, loadHistory } = chat;
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [removing, setRemoving] = useState<Dialog | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const rememberOpener = useFocusReturn(removing !== null || renamingId !== null);
  const viewer = useDocumentViewer();
  const { open: openViewer, close: closeViewer } = viewer;

  const openSource = useCallback(
    (source: Source, openerId: string) =>
      openViewer(
        { documentId: source.document_id, title: source.document_title, fragmentId: source.fragment_id },
        `[data-opener="${openerId}"]`,
      ),
    [openViewer],
  );

  // Просмотр относится к открытому чату: при переходе в другой он закрывается.
  useEffect(() => closeViewer, [id, closeViewer]);

  const key = id ?? chat.newKey;
  const view = id ? chat.state.dialogs[id] : undefined;
  const dialog = id ? history.items.find((item) => item.id === id) : undefined;
  const title = id ? (dialog?.title ?? labels.newLabel) : props.sectionTitle;
  const last = view?.messages.at(-1);
  const formingElsewhere = last?.status === 'streaming' && !view?.generation;
  usePageTitle(title);

  const historyIdle = history.status === 'idle';
  useEffect(() => {
    if (historyIdle) {
      loadHistory(false);
    }
  }, [historyIdle, loadHistory]);

  useEffect(() => {
    if (id) {
      openDialog(id);
    }
  }, [id, openDialog]);

  // Чат удалён (здесь или в другой вкладке): открывается пустой новый чат.
  const deleted = view?.status === 'deleted';
  useEffect(() => {
    if (deleted) {
      navigate(basePath, { replace: true });
    }
  }, [basePath, deleted, navigate]);

  // Ответ формируется в другой вкладке: сообщения перезапрашиваются, пока состояние не сменится.
  useEffect(() => {
    if (!id || !formingElsewhere) {
      return;
    }
    const timer = window.setInterval(() => refreshDialog(id), POLL_MS);
    return () => window.clearInterval(timer);
  }, [id, formingElsewhere, refreshDialog]);

  const newChat = () => navigate(basePath);
  const onCreated = (createdId: string) => navigate(`${basePath}/${createdId}`, { replace: true });

  const startRename = (target: Dialog, opener: string) => {
    rememberOpener(`[data-opener="${opener}"] [tabindex="0"]`);
    if (target.id !== id) {
      navigate(`${basePath}/${target.id}`);
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
      SingleToast.push(labels.removed);
    } catch {
      // Чат остаётся в истории.
      SingleToast.push(labels.removeFailed, { use: 'error' });
    } finally {
      setRemovePending(false);
      setRemoving(null);
    }
  };

  let body: React.ReactNode;
  if (view?.status === 'not_found') {
    body = (
      <EmptyState
        title={labels.notFound}
        text={t.notFound.text}
        action={{ label: labels.newLabel, onClick: newChat }}
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
    body = <EmptyState title={props.empty.title} text={props.empty.text} />;
  } else {
    body = (
      <MessageList
        dialogId={id}
        view={view}
        onRegenerate={() => (props.onRegenerate ? props.onRegenerate(id) : chat.regenerate(id))}
        nothingFoundText={props.nothingFoundText}
        onLoadOlder={() => chat.loadOlder(id)}
        onOpenSource={openSource}
        openingFragment={viewer.opening?.fragmentId ?? null}
      />
    );
  }

  const usable = view?.status !== 'not_found' && view?.status !== 'failed';
  const lastAnswer = last?.role === 'assistant' ? last : undefined;

  return (
    <div className={viewer.isOpen ? `${styles.screen} ${styles.withViewer}` : styles.screen}>
      {/* Пока открыт просмотр, панель раздела скрыта: ответ и первоисточник видны одновременно (§3.3). */}
      {viewer.isOpen || (
      <HistoryPanel
        history={history}
        basePath={basePath}
        newLabel={labels.newLabel}
        onExport={(target) => void exportDocx(target.id)}
        onNewChat={newChat}
        onRename={(target) => startRename(target, `history-${target.id}`)}
        onRemove={(target) => startRemove(target, `history-${target.id}`)}
        onShowMore={() => chat.loadHistory(true)}
        onRetry={() => chat.loadHistory(false)}
      />
      )}
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
                    <MenuItem onClick={() => void exportDocx(dialog.id)}>{t.menu.exportDocx}</MenuItem>
                    <MenuItem onClick={() => startRemove(dialog, 'header')}>{t.menu.remove}</MenuItem>
                  </Kebab>
                </Hint>
              </span>
            )}
          </PageHeader>
        )}
        {props.toolbar}
        {props.banner && <div className={styles.banner}>{props.banner}</div>}
        <div className={styles.body}>{body}</div>
        {usable && (
          <>
            {lastAnswer && lastAnswer.dropped_messages > 0 && (
              <div className={styles.longDialog}>
                <Notice
                  kind="info"
                  action={labels.longDialogAction ? { label: labels.newLabel, onClick: newChat } : undefined}
                >
                  {labels.longDialog}
                </Notice>
              </div>
            )}
            <Composer
              dialogId={id ?? null}
              draft={chat.state.drafts[key] ?? EMPTY_DRAFT}
              answer={view?.generation ? 'generating' : formingElsewhere ? 'waiting' : 'idle'}
              placeholder={props.placeholder}
              mono={props.mono}
              note={props.note}
              chat={
                kind === 'chat'
                  ? {
                      mode: chat.mode,
                      knowledge: chat.knowledge,
                      attach: config ? { hint: attachHint(config.chat), extensions: config.chat.attachment_extensions } : null,
                      onModeChange: chat.setMode,
                      onKnowledgeChange: chat.setKnowledge,
                      onAddFiles: (files) => chat.addFiles(key, files, onCreated),
                      onRetryFile: (fileKey) => chat.retryFile(key, fileKey),
                      onRemoveFile: (fileKey) => chat.removeFile(key, fileKey),
                    }
                  : undefined
              }
              onTextChange={(text) => chat.setText(key, text)}
              onSend={() => chat.send(key, props.params, onCreated, props.onSendRefused)}
              onStop={() => id && chat.stop(id)}
              onNotice={(notice) => chat.setNotice(key, notice)}
            />
          </>
        )}
      </div>
      {viewer.element}
      {removing && (
        <ConfirmModal
          title={labels.removeTitle(removing.title ?? labels.newLabel)}
          body={labels.removeBody}
          action={t.remove.action}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </div>
  );
}
