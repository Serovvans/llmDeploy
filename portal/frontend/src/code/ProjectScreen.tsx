import { IconCommentRectTextRegular16 } from '@skbkontur/icons/IconCommentRectTextRegular16';
import { IconMediaUiAPlayRegular16 } from '@skbkontur/icons/IconMediaUiAPlayRegular16';
import { IconSettingsGearRegular16 } from '@skbkontur/icons/IconSettingsGearRegular16';
import { IconTimeClockMoveBackRegular16 } from '@skbkontur/icons/IconTimeClockMoveBackRegular16';
import { Button, Hint, Kebab, Link, Loader, MenuItem, MenuSeparator, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { CodeConfig, CodeFile, CodeProject, CodeTarget, CodeWeb } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import { EmptyState } from '../components/EmptyState';
import { Notice } from '../components/Notice';
import { TitleHint } from '../components/TitleHint';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { formatSize, texts } from '../texts';
import { useThemeChoice } from '../theme/ThemeProvider';
import { ArchiveModal } from './ArchiveModal';
import { useAssistant } from './Assistant';
import { BuildPanel, firstTab, hasMessages, type BuildTab } from './BuildPanel';
import codeStyles from './Code.module.css';
import { useCode } from './CodeContext';
import { CodeEditor, type Fragment, type Jump } from './CodeEditor';
import type { CreatedState } from './CodeHome';
import { EditTab, type Decision } from './EditTab';
import { EDIT_TAB, EditorTabs, TAB_PANEL_ID, tabElementId } from './EditorTabs';
import { ProjectFiles } from './FileTree';
import { useMonaco } from './monaco/loader';
import { isWebKind, kindOf, kindTexts } from './kinds';
import { useProjectActions } from './projectActions';
import panelStyles from './ProjectPanel.module.css';
import styles from './ProjectScreen.module.css';
import { importLines, routeState, splitPath } from './rules';
import { Crumb } from './SectionLayout';
import { SettingsModal } from './SettingsModal';
import { readAssistantOpen, readTabs, storeAssistantOpen, storeTabs } from './storage';
import { BUILD_POLL_MS, type ProjectApi, useProject } from './useProject';
import { VersionsPanel } from './VersionsPanel';
import type { PreviewProblem } from './web/preview';
import { PreviewPane } from './web/PreviewPane';
import { useSqlCheck } from './web/SqlCheck';
import { WebExportModal } from './web/WebExportModal';
import { WebRoutePanel, type PreviewStation, type WebTab } from './web/WebRoutePanel';
import { fixQuestion, type SyntaxMessage } from './web/webRules';

const w = texts.workshop;
/** Проверка синтаксиса — через 500 мс после последнего нажатия (§5.13.19). */
const SYNTAX_DELAY_MS = 500;

type Column = 'assistant' | 'preview' | null;

/** Главный файл при первом открытии: первый файл не фабрики, иначе первый (§5.13.6). */
function mainFile(files: readonly { id: string; path: string }[]): string | null {
  return (files.find((file) => !/GpToolFactory\.cs$/i.test(file.path)) ?? files[0])?.id ?? null;
}

/** Последний способ ввода: строка про Ctrl+M — только после перехода в редактор клавиатурой (§9). */
let lastKeyboard = false;
if (typeof document !== 'undefined') {
  document.addEventListener('keydown', () => (lastKeyboard = true), true);
  document.addEventListener('pointerdown', () => (lastKeyboard = false), true);
}

/** Экран проекта `/code/:id` (концепция §5.13.2–§5.13.13). Создаётся заново для каждого проекта. */
export function ProjectScreen({ id }: { id: string }) {
  const { config } = useSession();
  const codeConfig = config?.code as CodeConfig;
  const { sectionOff } = useCode();
  const navigate = useNavigate();
  const state = useProject(id, codeConfig, sectionOff);
  const project = state.project;
  usePageTitle(project?.name ?? w.title);

  if (state.status === 'not_found') {
    return (
      <div className={styles.missing}>
        <EmptyState
          headingLevel="h1"
          title={w.project.notFound.title}
          text={w.project.notFound.text}
          action={{ label: w.allProjects, onClick: () => navigate('/code') }}
        />
      </div>
    );
  }
  if (!project) {
    return (
      <div className={styles.missing}>
        {state.status === 'failed' ? (
          <div className={styles.failed}>
            <Notice kind="error" action={{ label: texts.common.retry, onClick: () => void state.reload() }}>
              {texts.common.actionFailed}
            </Notice>
          </div>
        ) : (
          <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
            <div className={styles.loading} />
          </Loader>
        )}
      </div>
    );
  }
  return <ProjectView id={id} state={state} project={project} codeConfig={codeConfig} />;
}

interface ViewProps {
  id: string;
  state: ProjectApi;
  project: CodeProject;
  codeConfig: CodeConfig;
}

function ProjectView({ id, state, project, codeConfig }: ViewProps) {
  const { reloadConfig, config: portal } = useSession();
  const kind = kindOf(project);
  const isWeb = isWebKind(kind);
  const previewEnabled = isWeb && codeConfig.preview?.enabled === true;
  const { sdk, sectionOff, touchProject, forgetProject } = useCode();
  const navigate = useNavigate();
  const location = useLocation();
  const created = (location.state ?? null) as CreatedState | null;
  const { resolved: theme } = useThemeChoice();
  const monaco = useMonaco();
  const chat = useChat();
  const actions = useProjectActions();

  const [tabs, setTabs] = useState<string[]>([]);
  const [active, setActive] = useState<string | null>(null);
  const [editMessage, setEditMessage] = useState<string | null>(null);
  // «Помощник» и «Предпросмотр» — два вида одной колонки (§5.13.21); предпросмотр не запоминается.
  const [chosenColumn, setColumn] = useState<Column>(() => (readAssistantOpen() ? 'assistant' : null));
  const column = chosenColumn === 'preview' && !(isWeb && codeConfig.preview?.enabled === true) ? null : chosenColumn;
  const assistantOpen = column === 'assistant';
  const previewOpen = column === 'preview';
  const setAssistantOpen = (open: boolean) => setColumn(open ? 'assistant' : null);
  const [expanded, setExpanded] = useState(false);
  const [buildTab, setBuildTab] = useState<BuildTab>('errors');
  const [buildPending, setBuildPending] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [jump, setJump] = useState<Jump | null>(null);
  const [modal, setModal] = useState<'archive' | 'settings' | 'versions' | 'webExport' | null>(null);
  const rememberOpener = useFocusReturn(modal !== null);
  /** Окно открывается с кнопки или ссылки внутри `[data-opener=…]`: после закрытия фокус вернётся на неё (§9). */
  const openModal = (which: 'archive' | 'settings' | 'versions' | 'webExport', opener: string) => {
    rememberOpener(`[data-opener="${opener}"] :is(a, button)`);
    setModal(which);
  };
  const [webTab, setWebTab] = useState<WebTab>('syntax');
  const [codeWeb, setCodeWeb] = useState<CodeWeb | null>(null);
  const [syntax, setSyntax] = useState<SyntaxMessage[] | null>(null);
  const [edits, bumpEdits] = useReducer((n: number) => n + 1, 0);
  const syntaxSeq = useRef(0);
  const [runKey, setRunKey] = useState(0);
  const [previewExpanded, setPreviewExpanded] = useState(false);
  const [previewRun, setPreviewRun] = useState<Map<string, string> | null>(null);
  const [previewProblems, setPreviewProblems] = useState<PreviewProblem[]>([]);
  const [focusPreview, setFocusPreview] = useState(false);
  const [readOnly, setReadOnly] = useState(false);
  const [decisionPending, setDecisionPending] = useState<string | null>(null);
  const [report, setReport] = useState(created?.importReport ?? null);
  // The import report is kept on screen, but the address forgets it: a page reload does not bring it back.
  const hasReport = Boolean(created?.importReport);
  useEffect(() => {
    if (hasReport) {
      navigate(location.pathname, { replace: true, state: null });
    }
  }, [hasReport, location.pathname, navigate]);
  const [tabHint, setTabHint] = useState(false);
  // Куда перевести фокус после «Посмотреть и принять» и после решения по правке (§9).
  const [focusAfter, setFocusAfter] = useState<'edit' | 'editor' | { card: string } | null>(null);
  const focusDone = useCallback(() => setFocusAfter(null), []);
  const opened = useRef(false);

  const projectGone = useCallback(() => {
    SingleToast.push(w.project.removedElsewhere);
    forgetProject(id);
    navigate('/code', { replace: true });
  }, [forgetProject, id, navigate]);

  const { listen } = state;
  useEffect(() => {
    listen({
      projectGone,
      fileGone: (fileId) => {
        setTabs((current) => current.filter((item) => item !== fileId));
        setActive((current) => (current === fileId ? null : current));
      },
    });
  }, [listen, projectGone]);

  // Первое открытие: вкладки из браузера, иначе главный файл.
  useEffect(() => {
    if (!project || opened.current) {
      return;
    }
    opened.current = true;
    const stored = readTabs(project.id);
    const known = new Set(project.files.map((file) => file.id));
    const open = stored?.open.filter((fileId) => known.has(fileId)) ?? [];
    const first = open.length > 0 ? open : [mainFile(project.files)].filter((item): item is string => item !== null);
    setTabs(first);
    setActive(stored?.active && first.includes(stored.active) ? stored.active : (first[0] ?? null));
  }, [project]);

  useEffect(() => {
    if (opened.current) {
      storeTabs(id, { open: tabs, active: active === EDIT_TAB ? (tabs[0] ?? null) : active });
    }
  }, [active, id, tabs]);

  // Список проектов слева видит новое время изменения и итог сборки.
  useEffect(() => {
    if (project) {
      touchProject(project);
    }
  }, [project, touchProject]);

  // Пришла новая сборка (кнопкой здесь или в другой вкладке) — панель разворачивается при сообщениях.
  const buildKey = project?.build?.created_at ?? null;
  const seenBuild = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    if (!project) {
      return;
    }
    if (seenBuild.current !== undefined && buildKey !== seenBuild.current && project.build && hasMessages(project.build)) {
      setExpanded(true);
      setBuildTab(firstTab(project.build));
    }
    seenBuild.current = buildKey;
  }, [buildKey, project]);

  const view = project ? chat.state.dialogs[project.dialog_id] : undefined;
  const fixingHere = view?.generation?.phase === 'fixing';

  // Идёт сборка не этой кнопкой — проект перечитывается каждые 3 секунды (§5.13.8).
  const polling = Boolean(project?.build_running) && !buildPending && !fixingHere;
  const { reload } = state;
  useEffect(() => {
    if (!polling) {
      return;
    }
    const timer = window.setInterval(() => void reload(), BUILD_POLL_MS);
    return () => window.clearInterval(timer);
  }, [polling, reload]);

  const files = useMemo(() => project?.files ?? [], [project]);
  const fileTabs = tabs.flatMap((fileId) => {
    const file = files.find((item) => item.id === fileId);
    return file ? [{ id: file.id, path: file.path }] : [];
  });
  const activeFile = files.find((file) => file.id === active) ?? null;

  const openFile = (fileId: string) => {
    if (active && active !== fileId) {
      void state.flush();
    }
    setTabs((current) => (current.includes(fileId) ? current : [...current, fileId]));
    setActive(fileId);
  };

  const closeTab = (tabId: string) => {
    const order = [...(editMessage ? [EDIT_TAB] : []), ...tabs];
    const index = order.indexOf(tabId);
    const rest = order.filter((item) => item !== tabId);
    if (tabId === EDIT_TAB) {
      setEditMessage(null);
    } else {
      void state.flush();
      setTabs((current) => current.filter((item) => item !== tabId));
    }
    if (active === tabId) {
      setActive(rest[Math.min(index, rest.length - 1)] ?? null);
    }
  };

  const jumpTo = (fileId: string, line: number, column: number) => {
    openFile(fileId);
    setJump({ fileId, line, column, key: Date.now() });
  };

  const showAssistant = () => {
    setAssistantOpen(true);
    storeAssistantOpen(true);
  };

  // Описание `api` и библиотеки клиента CoGIS — один запрос при открытии проекта веб-вида (§7.5.16).
  useEffect(() => {
    if (!isWeb) {
      return;
    }
    let cancelled = false;
    api.getCodeWeb().then(
      (loaded) => !cancelled && setCodeWeb(loaded),
      (error: unknown) => {
        sectionOff(error);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [isWeb, sectionOff]);

  useEffect(() => {
    if (monaco.status === 'ready' && codeWeb) {
      void monaco.setup.configureWeb(codeWeb);
      monaco.setup.registerHtmlScriptHints();
    }
  }, [codeWeb, monaco]);

  const filesRef = useRef(files);
  useEffect(() => {
    filesRef.current = files;
  }, [files]);
  const { text: textOf } = state;

  // Проверка синтаксиса всех файлов проекта по мере набора (§5.13.19).
  const syntaxReady = syntax !== null;
  useEffect(() => {
    if (!isWeb || monaco.status !== 'ready') {
      return;
    }
    const setup = monaco.setup;
    const timer = window.setTimeout(
      () => {
        const seq = ++syntaxSeq.current;
        const current = filesRef.current.map((file) => ({ path: file.path, content: textOf(file.id) }));
        setup.checkSyntax(current).then(
          (result) => seq === syntaxSeq.current && setSyntax(result),
          () => undefined,
        );
      },
      syntaxReady ? SYNTAX_DELAY_MS : 0,
    );
    return () => window.clearTimeout(timer);
  }, [edits, files, isWeb, monaco, syntaxReady, textOf]);

  const onTextChange = (fileId: string, text: string) => {
    state.setText(fileId, text);
    if (isWeb) {
      bumpEdits();
    }
  };

  /** Сохранённые файлы с текстами редактора: несохранённая правка сначала сохраняется (§5.13.21–5.13.22). */
  const savedFiles = useCallback(async (): Promise<CodeFile[]> => {
    await state.flush();
    return filesRef.current.map((file) => ({ ...file, content: textOf(file.id) }));
  }, [state, textOf]);

  const showPreview = () => {
    if (previewOpen) {
      return;
    }
    setColumn('preview');
    setFocusPreview(true);
  };

  const runPreview = () => {
    if (previewOpen) {
      setRunKey((key) => key + 1);
    } else {
      showPreview();
    }
  };

  const previewOff = useCallback(() => {
    SingleToast.push(w.web.pane.off);
    setColumn((current) => (current === 'preview' ? null : current));
    setPreviewExpanded(false);
    reloadConfig();
  }, [reloadConfig]);

  // Предпросмотр выключили в настройках портала, пока колонка открыта (§5.13.21): колонка закрывается.
  const previewWasOpen = useRef(false);
  useEffect(() => {
    if (previewWasOpen.current && !previewEnabled) {
      SingleToast.push(w.web.pane.off);
    }
    previewWasOpen.current = previewEnabled && column === 'preview';
  }, [column, previewEnabled]);

  const decide = async (messageId: string, accept: { path: string; revision: number | null }[], total: number): Promise<Decision> => {
    setDecisionPending(messageId);
    setReadOnly(true);
    await state.flush();
    try {
      const result = await api.decideCodeEdit(id, messageId, accept);
      state.replace(result.project);
      if (project) {
        chat.refreshDialog(project.dialog_id);
      }
      const count = result.edit.files.filter((file) => file.decision === 'accepted').length;
      if (accept.length === 0) {
        SingleToast.push(w.edit.rejectedToast);
      } else {
        SingleToast.push(count === total ? w.edit.acceptedToast(count) : w.edit.acceptedPartToast(count, total));
      }
      const editClosing = editMessage === messageId;
      const fileShown = active !== null && active !== EDIT_TAB;
      if (editClosing) {
        closeTab(EDIT_TAB);
      }
      setFocusAfter(fileShown || (editClosing && tabs.length > 0) ? 'editor' : { card: messageId });
      return { kind: 'done' };
    } catch (error) {
      if (isApiError(error, 'edit_decided')) {
        if (project) {
          chat.refreshDialog(project.dialog_id);
        }
        return { kind: 'decided' };
      }
      if (isApiError(error, 'edit_outdated')) {
        const paths = Array.isArray(error.details.paths) ? error.details.paths.map(String) : [];
        return { kind: 'outdated', paths };
      }
      if (isApiError(error, 'project_limit')) {
        const max = Number(error.details.max);
        SingleToast.push(error.details.limit === 'files' ? w.edit.limitFiles(max) : w.edit.limitBytes(formatSize(max)), { use: 'error' });
      } else if (isApiError(error, 'not_found')) {
        projectGone();
      } else if (!sectionOff(error) && !isApiError(error, 'unauthenticated')) {
        SingleToast.push(texts.common.actionFailed, { use: 'error' });
      }
      return { kind: 'failed' };
    } finally {
      setDecisionPending(null);
      setReadOnly(false);
    }
  };

  const review = (messageId: string) => {
    void state.flush();
    setEditMessage(messageId);
    setActive(EDIT_TAB);
    setFocusAfter('edit');
  };

  // Без открытых файлов фокус после решения — на карточку правки в ленте помощника.
  useEffect(() => {
    if (focusAfter !== null && typeof focusAfter === 'object') {
      document.querySelector<HTMLElement>(`[data-edit-card="${focusAfter.card}"]`)?.focus();
    }
  }, [focusAfter]);

  const rejectEdit = (messageId: string) => {
    const edit = view?.messages.find((message) => message.id === messageId)?.code_edit;
    void decide(messageId, [], edit?.files.length ?? 0);
  };

  const openFragment = (target: CodeTarget) => {
    if (files.some((file) => file.id === target.file_id)) {
      jumpTo(target.file_id, target.start_line, 1);
    }
  };

  const answerFinished = useCallback(() => void reload(), [reload]);
  const assistant = useAssistant({
    project,
    config: codeConfig,
    sdkAvailable: sdk ? sdk.available : null,
    openFileIds: fileTabs.map((tab) => tab.id),
    activeFile,
    text: state.text,
    flush: state.flush,
    // Схема и база из настроек проекта входят в оценку памяти: после «Сохранить» она пересчитывается (§5.13.23).
    revision: `${state.revision}:${project?.schema_id ?? ''}:${JSON.stringify(project?.db ?? null)}`,
    visible: assistantOpen,
    show: showAssistant,
    reloadProject: () => void reload(),
    decisionPending,
    onOpenSettings: (opener) => openModal('settings', opener),
    onOpenVersions: (opener) => openModal('versions', opener),
    onReview: review,
    onRejectEdit: rejectEdit,
    onOpenFragment: openFragment,
    onAnswerFinished: answerFinished,
    sectionOff,
  });

  const sqlCheck = useSqlCheck({
    project,
    config: portal,
    sectionOff,
    reloadConfig,
    onOpenSettings: () => openModal('settings', 'settings'),
    onShowResult: () => {
      setExpanded(true);
      if (isWeb) {
        setWebTab('sql');
      } else {
        setBuildTab('sql');
      }
    },
  });

  const fixSyntax = () =>
    assistant.ask({ kind: 'write', content: fixQuestion('syntax', (syntax ?? []).map((item) => ({ path: item.path, line: item.line, message: item.message }))) });
  const previewErrors = previewProblems.filter((problem) => problem.kind !== 'image');
  const fixPreview = () =>
    assistant.ask({
      kind: 'write',
      content: fixQuestion(
        'preview',
        previewErrors.map((problem) => ({
          path: problem.file,
          line: problem.line,
          message: problem.kind === 'network' ? w.web.pane.network(problem.text) : problem.text,
        })),
      ),
    });
  const previewChanged = previewRun !== null && (previewRun.size !== files.length || files.some((file) => previewRun.get(file.path) !== textOf(file.id)));
  const previewStation: PreviewStation =
    previewRun === null ? 'none' : previewChanged ? 'changed' : previewErrors.length > 0 ? 'errors' : 'ok';

  // Мастер с описаниями: вопрос помощнику уходит сам один раз; адрес забывает его (§5.13.4).
  const wizardRequest = created?.assistantRequest ?? null;
  const wizardSent = useRef(false);
  const viewReady = view?.status === 'ready';
  useEffect(() => {
    if (!wizardRequest || wizardSent.current || !project || !viewReady) {
      return;
    }
    wizardSent.current = true;
    navigate(location.pathname, { replace: true, state: null });
    assistant.ask({ kind: 'wizard', content: wizardRequest });
  }, [assistant, location.pathname, navigate, project, viewReady, wizardRequest]);

  const runBuild = async () => {
    if (!project) {
      return;
    }
    if (files.length === 0) {
      SingleToast.push(w.build.noFiles);
      return;
    }
    setBuildPending(true);
    setUnavailable(false);
    await state.flush();
    try {
      state.setBuild(await api.buildCodeProject(id));
    } catch (error) {
      if (isApiError(error, 'build_in_progress')) {
        await reload();
      } else if (isApiError(error, 'build_unavailable')) {
        setUnavailable(true);
      } else if (isApiError(error, 'feature_disabled')) {
        // Сборку выключили, пока экран открыт: строка «Сборка на портале выключена…» придёт с настройками.
        reloadConfig();
        SingleToast.push(w.build.buildOff);
      } else if (isApiError(error, 'not_found')) {
        projectGone();
      } else if (!isApiError(error, 'unauthenticated')) {
        SingleToast.push(texts.common.actionFailed, { use: 'error' });
      }
    } finally {
      setBuildPending(false);
    }
  };
  const errorsByPath = new Map<string, number>();
  for (const item of project.build?.diagnostics ?? []) {
    if (item.severity === 'error' && item.file) {
      errorsByPath.set(item.file, (errorsByPath.get(item.file) ?? 0) + 1);
    }
  }
  const route = routeState({
    buildEnabled: codeConfig.build_enabled,
    build: project.build,
    pending: buildPending,
    running: project.build_running && !fixingHere,
    unavailable,
    changedLocally: state.changedSinceBuild,
  });
  const conflictFile = state.conflict ? files.find((file) => file.id === state.conflict?.fileId) : undefined;
  const imported = report ? importLines(report, project, codeConfig, sdk) : null;
  const showEdit = active === EDIT_TAB && editMessage !== null;
  const editorTab = showEdit ? EDIT_TAB : (activeFile?.id ?? null);

  let editorArea: React.ReactNode;
  if (monaco.status === 'failed') {
    editorArea = (
      <div className={styles.editorNotice}>
        <Notice kind="error" action={{ label: w.editor.reloadPage, onClick: () => window.location.reload() }}>
          {w.editor.failed}
        </Notice>
      </div>
    );
  } else if (monaco.status === 'loading') {
    editorArea = (
      <Loader active caption={w.editor.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else {
    editorArea = (
      <>
        {showEdit && editMessage && (
          <EditTab
            key={editMessage}
            setup={monaco.setup}
            theme={theme}
            projectId={id}
            messageId={editMessage}
            decide={(accept, total) => decide(editMessage, accept, total)}
            focusFirst={focusAfter === 'edit'}
            onFocused={focusDone}
          />
        )}
        {activeFile ? (
          <div
            className={showEdit ? styles.hidden : styles.editorHost}
            onFocus={() => setTabHint(lastKeyboard)}
            onBlur={() => setTabHint(false)}
          >
            <CodeEditor
              setup={monaco.setup}
              theme={theme}
              sdk={sdk}
              files={files}
              fileId={activeFile.id}
              text={state.text}
              onChange={onTextChange}
              build={project.build}
              syntax={isWeb ? syntax : undefined}
              onCheckSql={
                sqlCheck.enabled
                  ? (selected, fragment) => sqlCheck.open(selected, { path: fragment.path, startLine: fragment.start_line, endLine: fragment.end_line })
                  : undefined
              }
              readOnly={readOnly}
              jump={jump}
              focus={focusAfter === 'editor'}
              onFocused={focusDone}
              onExplain={(fragment: Fragment | null) => assistant.ask({ kind: 'explain', fragment })}
              onChangeSelection={(fragment) => assistant.ask({ kind: 'change', fragment })}
            />
          </div>
        ) : (
          !showEdit && <EmptyState title={w.editor.selectFile} text="" />
        )}
      </>
    );
  }

  const layout =
    previewOpen && previewExpanded
      ? `${styles.project} ${styles.previewExpanded}`
      : column !== null
        ? `${styles.project} ${styles.withAssistant}`
        : styles.project;

  return (
    <div className={layout}>
      <nav className={panelStyles.panel} aria-label={w.files.title}>
        <div className={panelStyles.back}>
          <Link
            href="/code"
            onClick={(event) => {
              event.preventDefault();
              navigate('/code');
            }}
          >
            ← {w.allProjects}
          </Link>
        </div>
        <ProjectFiles
          files={files}
          config={codeConfig}
          kind={kind}
          activeFileId={activeFile?.id ?? null}
          errors={errorsByPath}
          onOpen={openFile}
          create={state.createFile}
          rename={state.renameFile}
          remove={state.deleteFile}
          onRemoved={(fileId) => closeTab(fileId)}
        />
      </nav>
      <header className={styles.header}>
        <div className={codeStyles.titleRow}>
          <Crumb />
          <TitleHint text={project.name} className={styles.titleWrap}>
            <h1 className={codeStyles.title}>{project.name}</h1>
          </TitleHint>
          {isWeb && (
            <Hint text={w.web.where(kindTexts(kind).where)}>
              <span className={styles.kind} tabIndex={0}>
                · {kindTexts(kind).title}
              </span>
            </Hint>
          )}
        </div>
        <div className={styles.headerActions}>
          <span data-opener="versions">
            <Button size="small" icon={<IconTimeClockMoveBackRegular16 />} onClick={() => openModal('versions', 'versions')}>
              {w.project.versions}
            </Button>
          </span>
          <span data-opener="settings">
            <Button size="small" icon={<IconSettingsGearRegular16 />} onClick={() => openModal('settings', 'settings')}>
              {w.project.settings}
            </Button>
          </span>
          {previewEnabled && (
            <Hint text={previewOpen ? w.web.hidePreview : w.web.showPreview}>
              <Button
                size="small"
                icon={<IconMediaUiAPlayRegular16 />}
                active={previewOpen}
                aria-expanded={previewOpen}
                aria-controls={previewOpen ? 'preview-pane' : undefined}
                onClick={() => {
                  if (previewOpen) {
                    setColumn(null);
                    setPreviewExpanded(false);
                  } else {
                    showPreview();
                  }
                }}
              >
                {w.web.preview}
              </Button>
            </Hint>
          )}
          <Hint text={assistantOpen ? w.project.hideAssistant : w.project.showAssistant}>
            <Button
              size="small"
              icon={<IconCommentRectTextRegular16 />}
              active={assistantOpen}
              aria-expanded={assistantOpen}
              onClick={() => {
                setAssistantOpen(!assistantOpen);
                storeAssistantOpen(!assistantOpen);
                setPreviewExpanded(false);
              }}
            >
              {w.project.assistant}
            </Button>
          </Hint>
          <span data-opener="project-header">
            <Hint text={texts.users.actions} pos="left">
              <Kebab aria-label={`${texts.users.actions}: ${project.name}`}>
                {isWeb ? (
                  <MenuItem onClick={() => openModal('webExport', 'project-header')}>{w.web.exportCogis}</MenuItem>
                ) : (
                  <MenuItem onClick={() => actions.exportProject(project.id, project.name)}>{w.exportVs}</MenuItem>
                )}
                <MenuSeparator />
                <MenuItem
                  onClick={() => actions.askRemove({ id: project.id, name: project.name, kind }, '[data-opener="project-header"] [tabindex="0"]')}
                >
                  {w.removeProject}
                </MenuItem>
              </Kebab>
            </Hint>
          </span>
        </div>
      </header>
      <div className={styles.editor}>
        <EditorTabs
          tabs={fileTabs}
          edit={editMessage ? (view?.messages.find((message) => message.id === editMessage)?.code_edit?.files.length ?? 0) : null}
          active={editorTab}
          saveState={state.saveState}
          tabHint={tabHint}
          onSelect={(tabId) => (tabId === EDIT_TAB ? setActive(EDIT_TAB) : openFile(tabId))}
          onClose={closeTab}
        />
        {conflictFile && (
          <div className={styles.editorNotice}>
            <Notice
              kind="warning"
              action={[
                { label: w.editor.reload, onClick: () => state.resolveConflict('reload') },
                { label: w.editor.overwrite, onClick: () => state.resolveConflict('overwrite') },
              ]}
            >
              {w.editor.conflict(splitPath(conflictFile.path).name)}
            </Notice>
          </div>
        )}
        {imported && (
          <div className={styles.editorNotice}>
            <Notice kind="info" action={{ label: w.imported.hide, onClick: () => setReport(null) }}>
              {imported.lines.map((line) => (
                <span key={line} className={styles.reportLine}>
                  {line}
                  {line === w.imported.skipped(report?.skipped_total ?? 0) && (
                    <span className={styles.reportList}>
                      {imported.skipped.map((item) => (
                        <span key={item} className={styles.reportItem}>
                          {item}
                        </span>
                      ))}
                      {imported.more > 0 && <span>{w.imported.andMore(imported.more)}</span>}
                    </span>
                  )}
                </span>
              ))}
            </Notice>
          </div>
        )}
        {editorTab ? (
          <div className={styles.editorArea} role="tabpanel" id={TAB_PANEL_ID} aria-labelledby={tabElementId(editorTab)}>
            {editorArea}
          </div>
        ) : (
          <div className={styles.editorArea}>{editorArea}</div>
        )}
      </div>
      {isWeb ? (
        <WebRoutePanel
          syntax={syntax}
          previewEnabled={previewEnabled}
          preview={previewStation}
          previewErrors={previewErrors.length}
          previewOpen={previewOpen}
          expanded={expanded}
          tab={webTab}
          hasHtml={files.some((file) => file.path.toLowerCase().endsWith('.html'))}
          libraries={codeWeb?.libraries ?? null}
          sqlTab={sqlCheck.tab}
          paths={new Set(files.map((file) => file.path))}
          onToggle={(tab) => {
            if (tab) {
              setExpanded(true);
              setWebTab(tab);
            } else {
              setExpanded(!expanded);
            }
          }}
          onTab={setWebTab}
          onFixSyntax={fixSyntax}
          onFixPreview={fixPreview}
          onRunPreview={runPreview}
          onShowPreview={showPreview}
          onExport={() => openModal('webExport', 'web-export')}
          onJump={(path, line, column) => {
            const file = files.find((item) => item.path === path);
            if (file) {
              jumpTo(file.id, line, column);
            }
          }}
        />
      ) : (
        <BuildPanel
          state={route}
          build={project.build}
          expanded={expanded}
          tab={buildTab}
          paths={new Set(files.map((file) => file.path))}
          onToggle={(tab) => {
            if (tab) {
              setExpanded(true);
              setBuildTab(tab);
            } else {
              if (!expanded) {
                setBuildTab(firstTab(project.build));
              }
              setExpanded(!expanded);
            }
          }}
          onTab={setBuildTab}
          onBuild={() => void runBuild()}
          onFix={() => assistant.ask({ kind: 'fix' })}
          onDownload={() => openModal('archive', 'archive')}
          onExportVs={() => actions.exportProject(project.id, project.name)}
          sqlTab={sqlCheck.tab}
          onJump={(path, line, column) => {
            const file = files.find((item) => item.path === path);
            if (file) {
              jumpTo(file.id, line, column);
            }
          }}
        />
      )}
      {previewOpen && previewEnabled && (
        <PreviewPane
          project={project}
          web={codeWeb}
          limits={{ log_max_entries: codeConfig.preview?.log_max_entries ?? 500, errors_max: codeConfig.preview?.errors_max ?? 100 }}
          db={portal?.db ?? { enabled: false, tls_verify_available: false, sample_rows_check: 0, sample_rows_preview: 0, max_selected_tables: 0 }}
          runKey={runKey}
          prepare={async () => {
            try {
              return await savedFiles();
            } catch {
              return null;
            }
          }}
          changed={previewChanged}
          expanded={previewExpanded}
          onExpand={setPreviewExpanded}
          onRun={(ran) => setPreviewRun(new Map(ran.map((file) => [file.path, file.content])))}
          onProblems={setPreviewProblems}
          onJump={(path, line, column) => {
            const file = files.find((item) => item.path === path);
            if (file) {
              setPreviewExpanded(false);
              jumpTo(file.id, line, column);
            }
          }}
          onFeatureOff={previewOff}
          focusTitle={focusPreview}
        />
      )}
      {assistant.element}
      {assistant.viewer}
      {assistant.dialogs}
      {actions.modal}
      {sqlCheck.modal}
      {modal === 'webExport' && (
        <WebExportModal project={project} syntaxErrors={syntax?.length} savedFiles={savedFiles} onClose={() => setModal(null)} sectionOff={sectionOff} />
      )}
      {modal === 'archive' && project.build && (
        <ArchiveModal
          projectId={id}
          projectName={project.name}
          build={project.build}
          onClose={() => setModal(null)}
          onRefused={() => void reload()}
          sectionOff={sectionOff}
        />
      )}
      {modal === 'settings' && (
        <SettingsModal
          project={project}
          sdk={sdk}
          onClose={() => setModal(null)}
          onSaved={state.replace}
          onProjectGone={projectGone}
          sectionOff={sectionOff}
        />
      )}
      {modal === 'versions' && (
        <VersionsPanel
          projectId={id}
          max={codeConfig.max_snapshots}
          web={isWebKind(kindOf(project))}
          onClose={() => setModal(null)}
          beforeRestore={state.flush}
          onPending={setReadOnly}
          onRestored={(restored) => {
            state.replace(restored);
            const known = new Set(restored.files.map((file) => file.id));
            setTabs((current) => current.filter((fileId) => known.has(fileId)));
            setActive((current) => (current && current !== EDIT_TAB && !known.has(current) ? null : current));
          }}
          onProjectGone={projectGone}
          sectionOff={sectionOff}
        />
      )}
    </div>
  );
}

/** Маршрут `/code/:id`: экран создаётся заново при смене проекта. */
export function ProjectRoute() {
  const { id = '' } = useParams();
  return <ProjectScreen key={id} id={id} />;
}
