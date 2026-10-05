import { createContext, useContext, useMemo } from 'react';
import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

import type { Source, SqlCheck } from '../../api/types';
import { FOOTNOTE_MARK, markFootnotes, unmarkFootnotes } from '../../chat/footnotes';
import { texts } from '../../texts';
import { CodeBlock, SqlOrphanNotice } from './CodeBlock';
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
  position?: { start: { offset?: number } };
  children?: MdNode[];
  data?: { hName?: string; hProperties: Record<string, unknown> };
}

/**
 * Превращает метки засчитанных сносок в узлы кнопок. Где сноска — решает серверное правило
 * (`markFootnotes`), а не дерево Markdown. Метка, попавшая по разбору Markdown внутрь кода,
 * возвращается в вид `[n]`: кнопку там поставить нельзя.
 */
function sourceMarks() {
  let text = '';
  let codeLines: number[] = [];
  const split = (node: MdNode): MdNode[] => {
    const offset = node.position?.start.offset;
    if (node.type === 'code' && offset !== undefined) {
      // Строка открытия блока — по ней находится итог проверки (контракт §7.1). Строки считаются как на
      // сервере, только по `\n`: разбор Markdown считает концом строки и одиночный `\r`.
      const line = text.slice(0, offset).split('\n').length;
      node.data = { hProperties: { 'data-line': line } };
      codeLines.push(line);
    }
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
  return (tree: MdNode, file: { value: unknown }) => {
    text = String(file.value);
    codeLines = [];
    split(tree);
    // Записи проверки, которым не досталось блока, показываются под текстом ответа.
    tree.children?.push({
      type: 'sqlOrphans',
      data: { hName: 'sql-orphans', hProperties: { lines: codeLines.join(',') } },
    });
  };
}

const PLUGINS = [remarkGfm, sourceMarks];

const SqlCheckContext = createContext<SqlCheck | null>(null);

/** Блок кода с итогом проверки, если это блок `sql` проверенного ответа. */
function CheckedCodeBlock({ code, language, line }: { code: string; language: string | null; line?: number }) {
  const check = useContext(SqlCheckContext);
  const block = check?.blocks.find((item) => item.line === Number(line));
  return <CodeBlock code={code} language={language} check={block} />;
}

/** Итоги записей, для которых на их строке блока кода нет. */
function SqlOrphans({ lines }: { lines: string }) {
  const check = useContext(SqlCheckContext);
  const drawn = new Set(lines.split(',').map(Number));
  return check?.blocks
    .filter((block) => block.line === undefined || !drawn.has(block.line))
    .map((block) => <SqlOrphanNotice key={block.index} check={block} />);
}

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
    const props = (
      code as React.ReactElement<{ className?: string; children?: React.ReactNode; 'data-line'?: number }>
    ).props;
    const language = LANGUAGE_CLASS.exec(props.className ?? '')?.[1] ?? null;
    return (
      <CheckedCodeBlock
        code={textOf(props.children).replace(/\n$/, '')}
        language={language}
        line={props['data-line']}
      />
    );
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
  /** Итог проверки блоков `sql` — только у ответа SQL-помощника. */
  sqlCheck?: SqlCheck | null;
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

const COMPONENTS_WITH_MARKS = { ...COMPONENTS, 'source-mark': SourceMark, 'sql-orphans': SqlOrphans } as Components;

export function MarkdownView({ text, streaming = false, links, sqlCheck = null }: MarkdownViewProps) {
  const numbers = links?.sources.map((source) => source.n).join(',') ?? '';
  const marked = useMemo(
    () => (numbers ? markFootnotes(text, new Set(numbers.split(',').map(Number))) : text),
    [text, numbers],
  );

  return (
    <SourceLinksContext.Provider value={links ?? null}>
      <SqlCheckContext.Provider value={sqlCheck}>
      <div className={streaming ? `${styles.markdown} ${styles.streaming}` : styles.markdown}>
        <Markdown remarkPlugins={PLUGINS} components={COMPONENTS_WITH_MARKS}>
          {marked}
        </Markdown>
      </div>
      </SqlCheckContext.Provider>
    </SourceLinksContext.Provider>
  );
}
