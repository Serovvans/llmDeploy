import { useEffect, useRef, useState } from 'react';

import { texts } from '../texts';
import type { ResolvedTheme } from '../theme/preference';
import styles from './DiffView.module.css';

const t = texts.workshop.editor;
type Setup = typeof import('./monaco/setup');
type DiffEditor = ReturnType<Setup['monaco']['editor']['createDiffEditor']>;
type SideEditor = ReturnType<DiffEditor['getOriginalEditor']>;
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
  // Where the text of each column starts: the labels stand over the code, not over the line numbers.
  const [columns, setColumns] = useState<{ before: number; after: number } | null>(null);
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
    const textLeft = (side: SideEditor) => {
      const dom = side.getDomNode();
      return dom ? dom.getBoundingClientRect().left - node.getBoundingClientRect().left + side.getLayoutInfo().contentLeft : 0;
    };
    const place = () => setColumns({ before: textLeft(created.getOriginalEditor()), after: textLeft(created.getModifiedEditor()) });
    const layouts = [created.getOriginalEditor().onDidLayoutChange(place), created.getModifiedEditor().onDidLayoutChange(place)];
    let frame = 0;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) {
        setWide(entry.contentRect.width >= SIDE_BY_SIDE_MIN);
      }
      // The first layout of the inner editors happens before the listeners above exist.
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(place);
    });
    observer.observe(node);
    return () => {
      window.cancelAnimationFrame(frame);
      observer.disconnect();
      layouts.forEach((listener) => listener.dispose());
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
        <div className={`${styles.labels} ${styles.columns}`} aria-hidden="true">
          {columns && (
            <>
              <span style={{ left: columns.before }}>{t.before}</span>
              <span style={{ left: columns.after }}>{t.after}</span>
            </>
          )}
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
