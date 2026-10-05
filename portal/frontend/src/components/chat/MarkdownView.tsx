import { createContext, useContext, useMemo } from 'react';
import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import type { Source } from '../../api/types';
import { FOOTNOTE_MARK, markFootnotes, unmarkFootnotes } from '../../chat/footnotes';
import { texts } from '../../texts';
import { CodeBlock } from './CodeBlock';
import styles from './MarkdownView.module.css';

const LANGUAGE_CLASS = /language-(\S+)/;

function textOf(node: React.ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(textOf).join('');
  }
  return '';
}

/** Узел дерева Markdown — ровно то, что нужно для поиска сносок. */
interface MdNode {
  type: string;
  value?: string;
  children?: MdNode[];
  data?: { hName: string; hProperties: Record<string, unknown> };
}

/**
 * Превращает метки засчитанных сносок в узлы кнопок. Где сноска — решает серверное правило
 * (`markFootnotes`), а не дерево Markdown. Метка, попавшая по разбору Markdown внутрь кода,
 * возвращается в вид `[n]`: кнопку там поставить нельзя.
 */
function sourceMarks() {
  const split = (node: MdNode): MdNode[] => {
    if (node.type !== 'text' || !node.value) {
      if (node.value) {
        node.value = unmarkFootnotes(node.value);
      }
      if (node.children) {
        node.children = node.children.flatMap(split);
      }
      return [node];
    }
    const parts: MdNode[] = [];
    let last = 0;
    for (const match of node.value.matchAll(FOOTNOTE_MARK)) {
      parts.push({ type: 'text', value: node.value.slice(last, match.index) });
      parts.push({ type: 'sourceMark', data: { hName: 'source-mark', hProperties: { n: Number(match[1]) } } });
      last = match.index + match[0].length;
    }
    parts.push({ type: 'text', value: node.value.slice(last) });
    return parts.filter((part) => part.type !== 'text' || part.value);
  };
  return (tree: MdNode) => {
    split(tree);
  };
}

const PLUGINS = [remarkGfm, sourceMarks];

const COMPONENTS: Components = {
  // Ссылки из ответа — в новой вкладке, без передачи сведений о портале.
  a: ({ href, children }) => (
    <a href={href} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  ),
  // Картинки из ответа не загружаются: показывается адрес текстом.
  img: ({ src, alt }) => <span>{[alt, typeof src === 'string' ? src : ''].filter(Boolean).join(' — ')}</span>,
  // Блок кода: `pre` с единственным `code` внутри.
  pre: ({ children }) => {
    const code = Array.isArray(children) ? children[0] : children;
    const props = (code as React.ReactElement<{ className?: string; children?: React.ReactNode }>).props;
    const language = LANGUAGE_CLASS.exec(props.className ?? '')?.[1] ?? null;
    return <CodeBlock code={textOf(props.children).replace(/\n$/, '')} language={language} />;
  },
  table: ({ children }) => (
    <div className={styles.tableScroll}>
      <table className={styles.table}>{children}</table>
    </div>
  ),
};

/** Сноски ответа: источники, выделенный номер и действия (концепция §4.3). */
export interface SourceLinks {
  sources: Source[];
  active: number | null;
  /** Откуда вернуть фокус после просмотра: значение атрибута `data-opener` у сноски. */
  openerId: (n: number) => string;
  onActive: (n: number | null) => void;
  onOpen: (source: Source, openerId: string) => void;
}

interface MarkdownViewProps {
  text: string;
  /** Ответ ещё дописывается: в конце текста — черта. */
  streaming?: boolean;
  links?: SourceLinks;
}

/**
 * Текст ответа модели (концепция §4.2). Ответ — недоверенные данные: Markdown превращается
 * в элементы React, HTML из текста не исполняется и не вставляется в страницу — он виден как текст.
 */
const SourceLinksContext = createContext<SourceLinks | null>(null);

/** Кнопка-сноска. Данные берёт из контекста: тип компонента постоянен, кнопка не пересоздаётся при наведении. */
function SourceMark({ n }: { n: number }) {
  const links = useContext(SourceLinksContext);
  const source = links?.sources.find((item) => item.n === n);
  if (!links || !source) {
    return `[${n}]`;
  }
  const openerId = links.openerId(n);
  return (
    <button
      type="button"
      className={links.active === n ? `${styles.sourceMark} ${styles.sourceMarkActive}` : styles.sourceMark}
      data-opener={openerId}
      aria-label={texts.chat.sources.markLabel(n, source.document_title, source.page)}
      onMouseEnter={() => links.onActive(n)}
      onMouseLeave={() => links.onActive(null)}
      onFocus={() => links.onActive(n)}
      onBlur={() => links.onActive(null)}
      onClick={() => links.onOpen(source, openerId)}
    >
      [{n}]
    </button>
  );
}

const COMPONENTS_WITH_MARKS = { ...COMPONENTS, 'source-mark': SourceMark } as Components;

export function MarkdownView({ text, streaming = false, links }: MarkdownViewProps) {
  const numbers = links?.sources.map((source) => source.n).join(',') ?? '';
  const marked = useMemo(
    () => (numbers ? markFootnotes(text, new Set(numbers.split(',').map(Number))) : text),
    [text, numbers],
  );

  return (
    <SourceLinksContext.Provider value={links ?? null}>
      <div className={streaming ? `${styles.markdown} ${styles.streaming}` : styles.markdown}>
        <Markdown remarkPlugins={PLUGINS} components={COMPONENTS_WITH_MARKS}>
          {marked}
        </Markdown>
      </div>
    </SourceLinksContext.Provider>
  );
}
