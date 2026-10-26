import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Hint, Link, Select, SingleToast, Switcher } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { CodeAction, CodeConfig, CodeFile, CodeProject, CodeTarget, DbConnection, QuestionParams, Source } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { EMPTY_DRAFT, type ChatMessage } from '../chat/state';
import { Composer } from '../components/chat/Composer';
import { MessageList, type PhaseLabel } from '../components/chat/MessageList';
import { EmptyState } from '../components/EmptyState';
import { useDocumentViewer } from '../components/kb/DocumentViewer';
import { CarriedNotice } from '../components/memory/MemoryBlocks';
import { MemoryLayout } from '../components/memory/MemoryLayout';
import { Notice } from '../components/Notice';
import { useMemoryUi } from '../memory/useMemoryUi';
import { useSession } from '../session/SessionContext';
import { formatDate, texts } from '../texts';
import styles from './Assistant.module.css';
import type { Fragment } from './CodeEditor';
import { EditProposal } from './EditProposal';
import { isWebKind, kindOf } from './kinds';
import { hasEditBlock } from './rules';
import { readScope, storeScope, type Scope } from './storage';

const a = texts.workshop.assistant;
const web = texts.workshop.web;
const sc = texts.workshop.scope;
type Action = Exclude<CodeAction, 'fix'>;
const ACTIONS = (['write', 'explain'] as const).map((value) => ({ value, label: a.actions[value] }));

/** Вопрос, который строит интерфейс, а не поле ввода (§5.13.10). */
export type Ask =
  | { kind: 'explain'; fragment: Fragment | null }
  | { kind: 'fix' }
  | { kind: 'wizard'; content: string }
  | { kind: 'change'; fragment: Fragment }
  /** «Исправить с помощником» у JS и HTML (1.0, §5.13.20): обычный вопрос «Написать или изменить». */
  | { kind: 'write'; content: string };

/** Строки файла для `target` «весь файл»: последний перевод строки новой строки не начинает. */
function lineCount(text: string): number {
  const lines = text.split(/\r\n|\r|\n/);
  return Math.max(1, text.endsWith('\n') ? lines.length - 1 : lines.length);
}

function FragmentChip({ target, onOpen, onRemove }: { target: CodeTarget; onOpen?: () => void; onRemove?: () => void }) {
  const label = a.fragment(target.path, target.start_line, target.end_line);
  return (
    <span className={styles.chip}>
      {onOpen ? (
        <button type="button" className={styles.chipOpen} onClick={onOpen}>
          {label}
        </button>
      ) : (
        <span className={styles.chipText}>{label}</span>
      )}
      {onRemove && (
        <button type="button" className={styles.chipRemove} aria-label={a.removeFragment} onClick={onRemove}>
          <IconXRegular16 />
        </button>
      )}
    </span>
  );
}

interface Options {
  project: CodeProject;
  config: CodeConfig;
  sdkAvailable: boolean | null;
  /** Открытые вкладки файлов — без «Правки помощника». */
  openFileIds: readonly string[];
  activeFile: CodeFile | null;
  text: (fileId: string) => string;
  flush: () => Promise<boolean>;
  revision: string;
  visible: boolean;
  show: () => void;
  reloadProject: () => void;
  decisionPending: string | null;
  /** `opener` — значение `data-opener` у обёртки ссылки: на неё вернётся фокус. */
  onOpenSettings: (opener: string) => void;
  onOpenVersions: (opener: string) => void;
  onReview: (messageId: string) => void;
  onRejectEdit: (messageId: string) => void;
  onOpenFragment: (target: CodeTarget) => void;
  /** Ответ закончился: файлы и сборка могли измениться (правка, цикл исправления). */
  onAnswerFinished: () => void;
  sectionOff: (error: unknown) => boolean;
}

/** Колонка помощника проекта (концепция §5.13.7, §5.13.10, §5.13.12) и вопросы, которые строит интерфейс. */
export function useAssistant(options: Options) {
  const { project, config, openFileIds, activeFile } = options;
  const dialogId = project.dialog_id;
  const kind = kindOf(project);
  const isWeb = isWebKind(kind);
  const { config: portal } = useSession();
  // Выбор подключения проекта (1.0): при выключенном флаге подключений интерфейс его не показывает (§5.13.24).
  const db = portal?.db.enabled ? (project.db ?? null) : null;
  const [connection, setConnection] = useState<DbConnection | null>(null);
  const chat = useDialogs('code');
  const { openDialog, setNotice, setText } = chat;
  const navigate = useNavigate();
  const [action, setAction] = useState<Action>('write');
  const [fragment, setFragment] = useState<Fragment | null>(null);
  const [scope, setScopeState] = useState<Scope>(() => readScope(project.id));
  const [tooLarge, setTooLarge] = useState<Scope | null>(null);
  const [tooLong, setTooLong] = useState(false);
  const [documentation, setDocumentation] = useState<boolean | null>(null);
  const [wizardRunning, setWizardRunning] = useState(false);
  const [schemaName, setSchemaName] = useState<string | null>(null);
  const column = useRef<HTMLElement>(null);
  const viewer = useDocumentViewer();
  const { open: openViewer } = viewer;
  const view = chat.state.dialogs[dialogId];
  const last = view?.messages.at(-1);
  const formingElsewhere = last?.status === 'streaming' && !view?.generation;
  const busy = Boolean(view?.generation) || formingElsewhere;

  useEffect(() => {
    openDialog(dialogId);
  }, [dialogId, openDialog]);

  // Признак документации — при открытии проекта (§5.13.10); сбой запроса — заметки нет.
  useEffect(() => {
    let cancelled = false;
    api.getCogisDocumentation().then(
      ({ available }) => !cancelled && setDocumentation(available),
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // Имя схемы для строки «Видит» — из «Моих схем» сотрудника.
  const schemaId = project.schema_id;
  useEffect(() => {
    if (!schemaId) {
      return;
    }
    let cancelled = false;
    api.listSqlSchemas().then(
      ({ items }) => !cancelled && setSchemaName(items.find((item) => item.id === schemaId)?.name ?? null),
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [schemaId]);

  // Подключение проекта — для строки «Видит»: временно не используется ли оно и от какого числа схема.
  const connectionId = db?.available ? db.connection_id : null;
  useEffect(() => {
    if (!connectionId) {
      return;
    }
    let cancelled = false;
    api.listDbConnections().then(
      ({ items }) => !cancelled && setConnection(items.find((item) => item.id === connectionId) ?? null),
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [connectionId]);

  // Ответ закончился — экран перечитывает проект: правка или цикл исправления могли изменить сборку.
  const generating = Boolean(view?.generation);
  const wasGenerating = useRef(false);
  const { onAnswerFinished } = options;
  useEffect(() => {
    if (wasGenerating.current && !generating) {
      onAnswerFinished();
      setWizardRunning(false);
    }
    wasGenerating.current = generating;
  }, [generating, onAnswerFinished]);

  // Проект удалён в другой вкладке (событие потока `dialog_deleted`): раздел уходит на начало.
  const deleted = view?.status === 'deleted';
  useEffect(() => {
    if (deleted) {
      navigate('/code', { replace: true });
    }
  }, [deleted, navigate]);

  const setScope = (next: Scope) => {
    setScopeState(next);
    storeScope(project.id, next);
    setTooLarge(null);
  };

  const fileIds = scope === 'all' ? null : [...new Set(openFileIds)];
  const params: QuestionParams = {
    action,
    file_ids: fileIds,
    target: fragment ? { file_id: fragment.file_id, start_line: fragment.start_line, end_line: fragment.end_line } : null,
  };

  const memory = useMemoryUi({
    kind: 'code',
    dialogId,
    title: null,
    view,
    available: view?.status !== 'not_found' && view?.status !== 'failed',
    params,
    busy,
    viewer: { isOpen: viewer.isOpen, close: viewer.close },
    placement: 'feed',
    revision: options.revision,
    codeNote: isWeb ? (db ? web.memoryNoteDb : web.memoryNote) : db ? web.memoryNoteGptoolDb : undefined,
  });

  const onRefused = useCallback(
    (sentScope: Scope) =>
      (error: unknown): boolean => {
        if (isApiError(error, 'project_too_large')) {
          setTooLarge(sentScope);
          return true;
        }
        if (isApiError(error, 'message_too_long')) {
          setTooLong(true);
          return true;
        }
        if (isApiError(error, 'nothing_to_fix')) {
          SingleToast.push(a.nothingToFix);
          options.reloadProject();
          return true;
        }
        if (isApiError(error, 'generation_in_progress')) {
          SingleToast.push(a.busy);
          return true;
        }
        return options.sectionOff(error);
      },
    [options],
  );

  const sendBuilt = async (sendAction: CodeAction, content: string | undefined, target: Fragment | null) => {
    setTooLarge(null);
    setTooLong(false);
    await options.flush();
    const sendParams: QuestionParams = {
      action: sendAction,
      file_ids: fileIds,
      target: target ? { file_id: target.file_id, start_line: target.start_line, end_line: target.end_line } : null,
    };
    chat.send(dialogId, sendParams, () => undefined, onRefused(scope), {
      content,
      codeTarget: target,
    });
  };

  const wholeFile = (file: CodeFile): Fragment => ({
    file_id: file.id,
    path: file.path,
    start_line: 1,
    end_line: lineCount(options.text(file.id)),
  });

  /** Вопрос из меню редактора, «Пути до CoGIS» или мастера: идёт ответ — не уходит (§5.13.10). */
  const ask = (request: Ask) => {
    if (request.kind !== 'wizard' && busy) {
      SingleToast.push(a.busy);
      return;
    }
    options.show();
    switch (request.kind) {
      case 'explain': {
        setAction('explain');
        const target = request.fragment ?? (activeFile ? wholeFile(activeFile) : null);
        if (!target) {
          return;
        }
        void sendBuilt('explain', request.fragment ? a.explainFragment : a.explainFile(target.path), target);
        break;
      }
      case 'fix':
        void sendBuilt('fix', a.fixRequest, null);
        break;
      case 'wizard':
        setAction('write');
        setWizardRunning(true);
        void sendBuilt('write', request.content, null);
        break;
      case 'write':
        setAction('write');
        void sendBuilt('write', request.content, null);
        break;
      case 'change':
        setAction('write');
        setFragment(request.fragment);
        // Фрагмент встал в панель запроса — фокус в поле (§5.13.6).
        window.setTimeout(() => column.current?.querySelector('textarea')?.focus(), 0);
        break;
    }
  };

  const draft = chat.state.drafts[dialogId] ?? EMPTY_DRAFT;

  const onSend = () => {
    const typed = draft.text.trim();
    if (action === 'write' && !typed) {
      setNotice(dialogId, fragment ? a.enterFragment : a.enterWrite);
      return;
    }
    if (action === 'explain' && !typed) {
      if (fragment) {
        setFragment(null);
        void sendBuilt('explain', a.explainFragment, fragment);
      } else if (activeFile) {
        void sendBuilt('explain', a.explainFile(activeFile.path), wholeFile(activeFile));
      } else {
        setNotice(dialogId, a.openFile);
      }
      return;
    }
    setTooLarge(null);
    setTooLong(false);
    const target = fragment;
    setFragment(null);
    void options.flush().then(() =>
      chat.send(dialogId, params, () => undefined, onRefused(scope), { codeTarget: target }),
    );
  };

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

  const phaseLabel: PhaseLabel = (generation, message) => {
    const p = a.phases;
    if (generation.phase === 'fixing' && generation.fix) {
      const { stage, attempt, maxAttempts } = generation.fix;
      return stage === 'building' ? p.building(attempt + 1, maxAttempts + 1) : p.fixing(attempt + 1, maxAttempts + 1);
    }
    switch (generation.phase) {
      case 'sending':
      case 'searching':
        return wizardRunning ? p.wizard : p.searching;
      case 'preparing':
        return wizardRunning ? p.wizard : p.preparing;
      case 'answering':
        return hasEditBlock(message.content) ? p.editing : null;
      default:
        return null;
    }
  };

  const currentText = (path: string) => {
    const file = project.files.find((item) => item.path === path);
    return file ? options.text(file.id) : null;
  };

  const questionExtra = (message: ChatMessage) => {
    const target = message.code_target;
    if (!target) {
      return null;
    }
    const exists = project.files.some((file) => file.id === target.file_id);
    return <FragmentChip target={target} onOpen={exists ? () => options.onOpenFragment(target) : undefined} />;
  };

  const answerExtra = (message: ChatMessage) =>
    message.code_edit ? (
      <EditProposal
        projectId={project.id}
        messageId={message.id}
        edit={message.code_edit}
        config={config}
        kind={kind}
        currentText={currentText}
        pending={options.decisionPending === message.id}
        onReview={() => options.onReview(message.id)}
        onReject={() => options.onRejectEdit(message.id)}
        onVersions={options.onOpenVersions}
      />
    ) : null;

  let body: React.ReactNode;
  if (view?.status === 'failed') {
    body = (
      <div className={styles.failed}>
        <Notice kind="error" action={{ label: texts.common.retry, onClick: () => chat.refreshDialog(dialogId) }}>
          {texts.common.actionFailed}
        </Notice>
      </div>
    );
  } else if (!view || view.messages.length === 0) {
    body = view?.status === 'ready' ? <EmptyState title={a.empty.title} text={isWeb ? web.assistantEmpty : a.empty.text} /> : null;
  } else {
    body = (
      <MessageList
        dialogId={dialogId}
        view={view}
        narrow
        onRegenerate={() => chat.regenerate(dialogId, { onRefused: onRefused(scope) })}
        onLoadOlder={() => chat.loadOlder(dialogId)}
        onOpenSource={openSource}
        openingFragment={viewer.opening?.fragmentId ?? null}
        nothingFoundText={texts.cogis.nothingFound}
        memory={memory.feed}
        phaseLabel={phaseLabel}
        questionExtra={questionExtra}
        answerExtra={answerExtra}
        tooLongText={a.tooLong}
      />
    );
  }

  // У видов JS и HTML заметки о библиотеках C# нет (§5.13.17).
  const sdkMissing = !isWeb && options.sdkAvailable === false;
  const missing = documentation === false ? (sdkMissing ? a.noBoth : a.noDocumentation) : sdkMissing ? a.noSdk : null;
  let dbScope: React.ReactNode = null;
  if (db && !db.available) {
    dbScope = (
      <span data-opener="scope-schema">
        <Link component="button" use="grayed" onClick={() => options.onOpenSettings('scope-schema')}>
          {sc.dbUnavailable}
        </Link>
      </span>
    );
  } else if (db) {
    const unusable = connection?.usable === false;
    const date = connection?.snapshot ? formatDate(connection.snapshot.taken_at) : null;
    const link = (
      <span data-opener="scope-schema">
        <Link component="button" onClick={() => options.onOpenSettings('scope-schema')}>
          {unusable && date ? sc.tablesOld(db.title, db.tables.length, date) : sc.tables(db.title, db.tables.length)}
        </Link>
      </span>
    );
    dbScope = unusable ? <Hint text={sc.unusableHint}>{link}</Hint> : link;
  }
  const lastAnswer = last?.role === 'assistant' ? last : undefined;
  const longDialog = memory.active ? (
    memory.longDialog
  ) : lastAnswer && lastAnswer.dropped_messages > 0 ? (
    <Notice kind="info">{texts.dialogs.code.longDialog}</Notice>
  ) : null;
  const openCount = new Set(openFileIds).size;

  const element = options.visible && (
    <aside ref={column} className={styles.column} aria-label={a.title}>
      <div className={styles.head}>
        <h2 className={styles.title}>{a.title}</h2>
        {memory.toggle}
      </div>
      <div className={styles.scope}>
        <Hint text={isWeb ? web.scopeHint : texts.workshop.scope.hint} maxWidth={320}>
          <span className={styles.sees} tabIndex={0}>
            {texts.workshop.scope.sees}
          </span>
        </Hint>
        <Select<Scope>
          size="small"
          use="text"
          aria-label={texts.workshop.scope.label}
          value={scope}
          items={[
            ['all', texts.workshop.scope.all(project.files.length)],
            ['open', texts.workshop.scope.open(openCount)],
          ]}
          onValueChange={setScope}
        />
        {isWeb && (
          <>
            <span aria-hidden="true">·</span>
            <span>{web.scopeApi}</span>
          </>
        )}
        {dbScope && (
          <>
            <span aria-hidden="true">·</span>
            {dbScope}
          </>
        )}
        {!dbScope && project.schema_id && schemaName && (
          <>
            <span aria-hidden="true">·</span>
            <span data-opener="scope-schema">
              <Link component="button" onClick={() => options.onOpenSettings('scope-schema')}>{texts.workshop.scope.schema(schemaName)}</Link>
            </span>
          </>
        )}
        {!isWeb && options.sdkAvailable && (
          <>
            <span aria-hidden="true">·</span>
            <span>{texts.workshop.scope.reference}</span>
          </>
        )}
      </div>
      <MemoryLayout
        overlay
        panel={memory.panel}
        bottom={
          <div className={styles.bottom}>
            {longDialog && <div className={styles.notice}>{longDialog}</div>}
            {tooLarge && (
              <div className={styles.notice}>
                <Notice
                  kind="warning"
                  action={tooLarge === 'all' ? { label: a.onlyOpen, onClick: () => setScope('open') } : undefined}
                >
                  {tooLarge === 'all' ? a.tooLargeAll : a.tooLargeOpen}
                </Notice>
              </div>
            )}
            {tooLong && (
              <div className={styles.notice} data-opener="too-long">
                <Notice
                  kind="warning"
                  action={project.schema_id || db ? { label: a.settingsLink, onClick: () => options.onOpenSettings('too-long') } : undefined}
                >
                  {a.tooLong}
                </Notice>
              </div>
            )}
            <Composer
              dialogId={dialogId}
              draft={draft}
              narrow
              answer={view?.generation ? 'generating' : formingElsewhere ? 'waiting' : 'idle'}
              placeholder={fragment && action === 'write' ? a.placeholders.fragment : a.placeholders[action]}
              mono={action === 'explain'}
              waitText={a.busy}
              meter={memory.meter}
              controls={
                <Switcher
                  size="small"
                  items={ACTIONS}
                  value={action}
                  onValueChange={(value) => setAction(value as Action)}
                />
              }
              chips={
                fragment && (
                  <FragmentChip
                    target={fragment}
                    onOpen={() => options.onOpenFragment(fragment)}
                    onRemove={() => setFragment(null)}
                  />
                )
              }
              onTextChange={(next) => {
                setText(dialogId, next);
              }}
              onSend={onSend}
              onStop={() => chat.stop(dialogId)}
              onNotice={(notice) => setNotice(dialogId, notice)}
            />
          </div>
        }
      >
        {missing && (
          <div className={styles.notice}>
            <Notice
              kind="warning"
              action={
                documentation === false
                  ? { label: a.addDocumentation, onClick: () => navigate('/knowledge', { state: { addCogisDocumentation: true } }) }
                  : undefined
              }
            >
              {missing}
            </Notice>
          </div>
        )}
        {view?.carried && (
          <div className={styles.notice}>
            <CarriedNotice carried={view.carried} />
          </div>
        )}
        <div className={styles.feed}>{body}</div>
      </MemoryLayout>
    </aside>
  );

  return { ask, element, viewer: viewer.element, dialogs: memory.dialogs, viewerOpen: viewer.isOpen };
}
