import { useEffect, useRef } from 'react';

import type { CodeBuild, CodeFile, CodeSdk } from '../api/types';
import { texts } from '../texts';
import type { ResolvedTheme } from '../theme/preference';
import styles from './CodeEditor.module.css';
import type { Monaco } from './monaco/setup';
import { languageOf, type SyntaxMessage } from './web/webRules';

const t = texts.workshop.editor;
type Setup = typeof import('./monaco/setup');
type Editor = ReturnType<Monaco['editor']['create']>;
type Model = NonNullable<ReturnType<Editor['getModel']>>;
type ViewState = ReturnType<Editor['saveViewState']>;

/** Фрагмент для помощника: строки с 1, путь — на момент выделения. */
export interface Fragment {
  file_id: string;
  path: string;
  start_line: number;
  end_line: number;
}

/** Переход к месту сообщения сборки: `key` меняется при каждом нажатии. */
export interface Jump {
  fileId: string;
  line: number;
  column: number;
  key: number;
}

interface Props {
  setup: Setup;
  theme: ResolvedTheme;
  sdk: CodeSdk | null;
  files: readonly CodeFile[];
  fileId: string;
  /** Текст файла с несохранёнными правками — из состояния проекта. */
  text: (fileId: string) => string;
  onChange: (fileId: string, text: string) => void;
  build: CodeBuild | null;
  readOnly: boolean;
  jump: Jump | null;
  /** Перевести фокус в редактор (после решения по правке, §9); `onFocused` — запрос выполнен. */
  focus: boolean;
  onFocused: () => void;
  onExplain: (fragment: Fragment | null) => void;
  onChangeSelection: (fragment: Fragment) => void;
  /** Ошибки синтаксиса JS, HTML и CSS (1.0, §5.13.19): подчёркивания по всем открытым файлам. */
  syntax?: SyntaxMessage[] | null;
  /** «Проверить запрос» в меню (1.0, §5.13.23): только при включённой проверке SQL. */
  onCheckSql?: (text: string, fragment: Fragment) => void;
}

const URI_PREFIX = 'portal-file:/';

function markersFor(monaco: Monaco, build: CodeBuild | null, path: string, model: Model) {
  if (!build) {
    return [];
  }
  const lines = model.getLineCount();
  const place = (line: number, column: number | null, endLine: number | null, endColumn: number | null) => {
    const start = Math.min(line, lines);
    const end = Math.min(endLine ?? start, lines);
    return {
      startLineNumber: start,
      startColumn: column ?? 1,
      endLineNumber: end,
      // Без конца — до конца строки: волнистая линия видна, даже если столбец не указан.
      endColumn: endColumn ?? model.getLineMaxColumn(end),
    };
  };
  const diagnostics = build.diagnostics
    .filter((item) => item.file === path && item.line !== null)
    .map((item) => ({
      ...place(item.line as number, item.column, item.end_line, item.end_column),
      severity: item.severity === 'error' ? monaco.MarkerSeverity.Error : monaco.MarkerSeverity.Warning,
      message: item.message,
    }));
  const findings = (build.findings ?? [])
    .filter((item) => item.file === path && item.line !== null)
    .map((item) => ({
      ...place(item.line as number, item.column, null, null),
      severity: monaco.MarkerSeverity.Error,
      message: t.rulePrefix + item.message,
    }));
  return [...diagnostics, ...findings];
}

function syntaxMarkers(monaco: Monaco, syntax: readonly SyntaxMessage[] | null | undefined, path: string) {
  return (syntax ?? [])
    .filter((item) => item.path === path)
    .map((item) => ({
      startLineNumber: item.line,
      startColumn: item.column,
      endLineNumber: item.endLine,
      endColumn: item.endColumn,
      severity: monaco.MarkerSeverity.Error,
      message: item.message,
    }));
}

/**
 * Редактор Monaco (концепция §5.13.6): одна модель на файл, вкладки меняют модель; подчёркивания —
 * сообщения последней сборки, поставленные при её приходе и дальше движущиеся вместе с текстом.
 */
export function CodeEditor(props: Props) {
  const { setup, theme, sdk, files, fileId, build, readOnly, jump, focus, onFocused } = props;
  const monaco = setup.monaco;
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<Editor | null>(null);
  const models = useRef(new Map<string, Model>());
  const views = useRef(new Map<string, ViewState>());
  const latest = useRef(props);
  const flash = useRef<{ clear: () => void } | null>(null);
  const markedBuild = useRef<string | null>(null);

  useEffect(() => {
    latest.current = props;
  });

  const pathOf = (id: string) => latest.current.files.find((file) => file.id === id)?.path ?? '';

  const fragment = (): Fragment | null => {
    const current = editor.current;
    const selection = current?.getSelection();
    if (!current || !selection || selection.isEmpty()) {
      return null;
    }
    // Выделение до начала следующей строки — без неё.
    const end = selection.endColumn === 1 && selection.endLineNumber > selection.startLineNumber ? selection.endLineNumber - 1 : selection.endLineNumber;
    const id = latest.current.fileId;
    return { file_id: id, path: pathOf(id), start_line: selection.startLineNumber, end_line: end };
  };

  // Редактор создаётся один раз на экран проекта.
  useEffect(() => {
    const node = host.current;
    if (!node) {
      return;
    }
    const created = monaco.editor.create(node, {
      model: null,
      automaticLayout: true,
      fixedOverflowWidgets: true,
      minimap: { enabled: false },
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--p-font-mono').trim() || 'monospace',
      fontSize: 14,
      lineHeight: 22,
      fontLigatures: false,
      tabSize: 4,
      insertSpaces: true,
      detectIndentation: false,
      links: false,
      codeLens: false,
      inlayHints: { enabled: 'off' },
      lightbulb: { enabled: monaco.editor.ShowLightbulbIconMode.Off },
      scrollBeyondLastLine: false,
      renderLineHighlight: 'line',
      contextmenu: true,
    });
    editor.current = created;
    setup.remeasureWhenFontsReady();
    created.addAction({
      id: 'portal.explain',
      label: t.explain,
      contextMenuGroupId: '0_portal',
      contextMenuOrder: 1,
      run: () => latest.current.onExplain(fragment()),
    });
    created.addAction({
      id: 'portal.changeSelection',
      label: t.changeSelection,
      contextMenuGroupId: '0_portal',
      contextMenuOrder: 2,
      precondition: 'editorHasSelection',
      run: () => {
        const selected = fragment();
        if (selected) {
          latest.current.onChangeSelection(selected);
        }
      },
    });
    const listener = created.onDidChangeModelContent(() => {
      const model = created.getModel();
      if (model) {
        latest.current.onChange(model.uri.path.slice(1), model.getValue());
      }
    });
    const known = models.current;
    return () => {
      listener.dispose();
      created.dispose();
      known.forEach((model) => model.dispose());
      known.clear();
      editor.current = null;
    };
    // `fragment` и `pathOf` читают `latest` — пересоздавать редактор не нужно.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monaco, setup]);

  useEffect(() => {
    setup.applyTheme(theme);
  }, [setup, theme]);

  useEffect(() => {
    if (sdk) {
      setup.setSdkTypes(sdk.types);
    }
  }, [setup, sdk]);

  // Вкладка: модель файла создаётся при первом открытии, положение курсора запоминается.
  useEffect(() => {
    const current = editor.current;
    if (!current) {
      return;
    }
    const previous = current.getModel();
    if (previous) {
      views.current.set(previous.uri.path.slice(1), current.saveViewState());
    }
    let model = models.current.get(fileId);
    if (!model) {
      model = monaco.editor.createModel(latest.current.text(fileId), languageOf(pathOf(fileId)), monaco.Uri.parse(URI_PREFIX + fileId));
      models.current.set(fileId, model);
      monaco.editor.setModelMarkers(model, 'build', markersFor(monaco, latest.current.build, pathOf(fileId), model));
      monaco.editor.setModelMarkers(model, 'syntax', syntaxMarkers(monaco, latest.current.syntax, pathOf(fileId)));
    }
    if (previous !== model) {
      current.setModel(model);
      const view = views.current.get(fileId);
      if (view) {
        current.restoreViewState(view);
      }
    }
    current.updateOptions({ ariaLabel: t.label(pathOf(fileId)) });
     
  }, [fileId, monaco]);

  // Текст пришёл с сервера (принятая правка, возврат версии, «Загрузить с портала»): модель догоняет его.
  useEffect(() => {
    for (const [id, model] of models.current) {
      if (!files.some((file) => file.id === id)) {
        model.dispose();
        models.current.delete(id);
        views.current.delete(id);
        continue;
      }
      const text = latest.current.text(id);
      if (model.getValue() !== text) {
        model.pushEditOperations([], [{ range: model.getFullModelRange(), text }], () => null);
      }
      // Переименование с другим расширением (у «Каталога карт») меняет язык файла.
      const language = languageOf(pathOf(id));
      if (model.getLanguageId() !== language) {
        monaco.editor.setModelLanguage(model, language);
      }
    }
    editor.current?.updateOptions({ ariaLabel: t.label(pathOf(latest.current.fileId)) });
     
  }, [files, monaco]);

  // Новая сборка — подчёркивания заменяются во всех открытых файлах.
  useEffect(() => {
    const key = build?.created_at ?? null;
    if (markedBuild.current === key) {
      return;
    }
    markedBuild.current = key;
    for (const [id, model] of models.current) {
      monaco.editor.setModelMarkers(model, 'build', markersFor(monaco, build, pathOf(id), model));
    }
     
  }, [build, monaco]);

  // Новый итог проверки синтаксиса — подчёркивания заменяются во всех открытых файлах.
  const { syntax } = props;
  useEffect(() => {
    for (const [id, model] of models.current) {
      monaco.editor.setModelMarkers(model, 'syntax', syntaxMarkers(monaco, syntax, pathOf(id)));
    }
  }, [syntax, monaco]);

  // «Проверить запрос» — третьим пунктом меню, только при выделении и включённой проверке SQL.
  const checkSql = Boolean(props.onCheckSql);
  useEffect(() => {
    const current = editor.current;
    if (!current || !checkSql) {
      return;
    }
    const action = current.addAction({
      id: 'portal.checkSql',
      label: texts.workshop.web.sqlCheck.menu,
      contextMenuGroupId: '0_portal',
      contextMenuOrder: 3,
      precondition: 'editorHasSelection',
      run: () => {
        const selected = fragment();
        const selection = current.getSelection();
        const model = current.getModel();
        if (selected && selection && model) {
          latest.current.onCheckSql?.(model.getValueInRange(selection), selected);
        }
      },
    });
    return () => action.dispose();
    // `fragment` reads `latest`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkSql]);

  useEffect(() => {
    editor.current?.updateOptions({ readOnly });
  }, [readOnly]);

  // Переход к сообщению сборки: курсор, строка в центре, подсветка 2 секунды, фокус в редактор (§5.13.8).
  useEffect(() => {
    const current = editor.current;
    if (!current || !jump || jump.fileId !== fileId) {
      return;
    }
    const model = current.getModel();
    if (!model) {
      return;
    }
    const line = Math.min(jump.line, model.getLineCount());
    current.setPosition({ lineNumber: line, column: jump.column });
    current.revealLineInCenter(line);
    flash.current?.clear();
    const decorations = current.createDecorationsCollection([
      { range: new monaco.Range(line, 1, line, 1), options: { isWholeLine: true, className: 'p-flash-line' } },
    ]);
    const timer = window.setTimeout(() => decorations.clear(), 2000);
    flash.current = {
      clear: () => {
        window.clearTimeout(timer);
        decorations.clear();
      },
    };
    current.focus();
  }, [fileId, jump, monaco]);

  useEffect(() => {
    if (focus && editor.current) {
      editor.current.focus();
      onFocused();
    }
  }, [focus, onFocused]);

  // The host is the skip link's target (§9): focusing it hands focus over to Monaco.
  // `monaco-component` gives the context menu (rendered next to `.monaco-editor`) the theme's colour variables.
  return (
    <div
      ref={host}
      className={`${styles.editor} monaco-component`}
      data-testid="code-editor"
      data-skip-target=""
      tabIndex={-1}
      onFocus={(event) => {
        if (event.target === event.currentTarget) {
          editor.current?.focus();
        }
      }}
    />
  );
}
