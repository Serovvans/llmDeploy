import { useEffect, useRef, useState } from 'react';

import { texts } from '../texts';
import type { ResolvedTheme } from '../theme/preference';
import styles from './DiffView.module.css';

const t = texts.workshop.editor;
type Setup = typeof import('./monaco/setup');
type DiffEditor = ReturnType<Setup['monaco']['editor']['createDiffEditor']>;
/** Две колонки «Было» / «Стало» — когда область редактора от 900 px (§5.13.11). */
const SIDE_BY_SIDE_MIN = 900;

interface Props {
  setup: Setup;
  theme: ResolvedTheme;
  path: string;
  before: string;
  after: string;
}

/**
 * Разница правки помощника (концепция §5.13.11): Monaco в режиме сравнения, только для чтения.
 * «Было» — текущий файл проекта, «Стало» — предложение; знаки «−» и «+» на полях есть всегда.
 */
export function DiffView({ setup, theme, path, before, after }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<DiffEditor | null>(null);
  const [wide, setWide] = useState(true);
  const monaco = setup.monaco;

  useEffect(() => {
    const node = host.current;
    if (!node) {
      return;
    }
    const created = monaco.editor.createDiffEditor(node, {
      readOnly: true,
      originalEditable: false,
      automaticLayout: true,
      fixedOverflowWidgets: true,
      renderIndicators: true,
      renderSideBySide: true,
      useInlineViewWhenSpaceIsLimited: false,
      renderOverviewRuler: false,
      minimap: { enabled: false },
      fontFamily: getComputedStyle(document.documentElement).getPropertyValue('--p-font-mono').trim() || 'monospace',
      fontSize: 14,
      lineHeight: 22,
      fontLigatures: false,
      scrollBeyondLastLine: false,
      links: false,
      contextmenu: true,
    });
    editor.current = created;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) {
        setWide(entry.contentRect.width >= SIDE_BY_SIDE_MIN);
      }
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
      const model = created.getModel();
      created.dispose();
      model?.original.dispose();
      model?.modified.dispose();
      editor.current = null;
    };
  }, [monaco]);

  useEffect(() => {
    setup.applyTheme(theme);
  }, [setup, theme]);

  useEffect(() => {
    const current = editor.current;
    if (!current) {
      return;
    }
    const previous = current.getModel();
    current.setModel({
      original: monaco.editor.createModel(before, 'csharp'),
      modified: monaco.editor.createModel(after, 'csharp'),
    });
    previous?.original.dispose();
    previous?.modified.dispose();
    current.getOriginalEditor().updateOptions({ ariaLabel: `${t.diffLabel(path)}. ${t.before}` });
    current.getModifiedEditor().updateOptions({ ariaLabel: `${t.diffLabel(path)}. ${t.after}` });
  }, [after, before, monaco, path]);

  useEffect(() => {
    editor.current?.updateOptions({ renderSideBySide: wide });
  }, [wide]);

  return (
    <div className={styles.view}>
      {wide ? (
        <div className={styles.labels} aria-hidden="true">
          <span>{t.before}</span>
          <span>{t.after}</span>
        </div>
      ) : (
        <div className={styles.labels} aria-hidden="true">
          <span>{texts.workshop.edit.legend}</span>
        </div>
      )}
      <div ref={host} className={styles.editor} />
    </div>
  );
}
