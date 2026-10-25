import { IconCommentRectTextRegular16 } from '@skbkontur/icons/IconCommentRectTextRegular16';
import { IconSettingsGearRegular16 } from '@skbkontur/icons/IconSettingsGearRegular16';
import { IconTimeClockMoveBackRegular16 } from '@skbkontur/icons/IconTimeClockMoveBackRegular16';
import { Button, Hint, Kebab, Link, Loader, MenuItem, MenuSeparator, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { CodeConfig, CodeProject, CodeTarget } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import { EmptyState } from '../components/EmptyState';
import { Notice } from '../components/Notice';
import { TitleHint } from '../components/TitleHint';
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
import { EDIT_TAB, EditorTabs } from './EditorTabs';
import { ProjectFiles } from './FileTree';
import { useMonaco } from './monaco/loader';
import { useProjectActions } from './projectActions';
import panelStyles from './ProjectPanel.module.css';
import styles from './ProjectScreen.module.css';
import { importLines, routeState, splitPath } from './rules';
import { Crumb } from './SectionLayout';
import { SettingsModal } from './SettingsModal';
import { readAssistantOpen, readTabs, storeAssistantOpen, storeTabs } from './storage';
import { BUILD_POLL_MS, type ProjectApi, useProject } from './useProject';
import { VersionsPanel } from './VersionsPanel';

const w = texts.workshop;

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
  const { reloadConfig } = useSession();
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
  const [assistantOpen, setAssistantOpen] = useState(readAssistantOpen);
  const [expanded, setExpanded] = useState(false);
  const [buildTab, setBuildTab] = useState<BuildTab>('errors');
  const [buildPending, setBuildPending] = useState(false);
  const [unavailable, setUnavailable] = useState(false);
  const [jump, setJump] = useState<Jump | null>(null);
  const [modal, setModal] = useState<'archive' | 'settings' | 'versions' | null>(null);
  const [readOnly, setReadOnly] = useState(false);
  const [decisionPending, setDecisionPending] = useState<string | null>(null);
  const [report, setReport] = useState(created?.importReport ?? null);
  const [tabHint, setTabHint] = useState(false);
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
      if (editMessage === messageId) {
        closeTab(EDIT_TAB);
      }
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
  };

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
    revision: state.revision,
    visible: assistantOpen,
    show: showAssistant,
    reloadProject: () => void reload(),
    decisionPending,
    onOpenSettings: () => setModal('settings'),
    onOpenVersions: () => setModal('versions'),
    onReview: review,
    onRejectEdit: rejectEdit,
    onOpenFragment: openFragment,
    onAnswerFinished: answerFinished,
    sectionOff,
  });

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
              onChange={state.setText}
              build={project.build}
              readOnly={readOnly}
              jump={jump}
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

  const layout = [styles.project, assistantOpen ? styles.withAssistant : '', expanded && route.kind !== 'disabled' ? styles.buildOpen : '']
    .filter(Boolean)
    .join(' ');

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
        </div>
        <div className={styles.headerActions}>
          <Button size="small" icon={<IconTimeClockMoveBackRegular16 />} onClick={() => setModal('versions')}>
            {w.project.versions}
          </Button>
          <Button size="small" icon={<IconSettingsGearRegular16 />} onClick={() => setModal('settings')}>
            {w.project.settings}
          </Button>
          <Hint text={assistantOpen ? w.project.hideAssistant : w.project.showAssistant}>
            <Button
              size="small"
              icon={<IconCommentRectTextRegular16 />}
              active={assistantOpen}
              aria-expanded={assistantOpen}
              onClick={() => {
                setAssistantOpen(!assistantOpen);
                storeAssistantOpen(!assistantOpen);
              }}
            >
              {w.project.assistant}
            </Button>
          </Hint>
          <span data-opener="project-header">
            <Hint text={texts.users.actions} pos="left">
              <Kebab aria-label={`${texts.users.actions}: ${project.name}`}>
                <MenuItem onClick={() => actions.exportProject(project.id, project.name)}>{w.exportVs}</MenuItem>
                <MenuSeparator />
                <MenuItem onClick={() => actions.askRemove({ id: project.id, name: project.name })}>{w.removeProject}</MenuItem>
              </Kebab>
            </Hint>
          </span>
        </div>
      </header>
      <div className={styles.editor}>
        <EditorTabs
          tabs={fileTabs}
          edit={editMessage ? (view?.messages.find((message) => message.id === editMessage)?.code_edit?.files.length ?? 0) : null}
          active={showEdit ? EDIT_TAB : (activeFile?.id ?? null)}
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
                  {line === w.imported.skipped(report?.skipped_files.length ?? 0) && (
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
        <div className={styles.editorArea}>{editorArea}</div>
      </div>
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
        onDownload={() => setModal('archive')}
        onExportVs={() => actions.exportProject(project.id, project.name)}
        onJump={(path, line, column) => {
          const file = files.find((item) => item.path === path);
          if (file) {
            jumpTo(file.id, line, column);
          }
        }}
      />
      {assistant.element}
      {assistant.viewer}
      {assistant.dialogs}
      {actions.modal}
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
