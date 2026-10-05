import Markdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';

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

interface MarkdownViewProps {
  text: string;
  /** Ответ ещё дописывается: в конце текста — черта. */
  streaming?: boolean;
}

/**
 * Текст ответа модели (концепция §4.2). Ответ — недоверенные данные: Markdown превращается
 * в элементы React, HTML из текста не исполняется и не вставляется в страницу — он виден как текст.
 */
export function MarkdownView({ text, streaming = false }: MarkdownViewProps) {
  return (
    <div className={streaming ? `${styles.markdown} ${styles.streaming}` : styles.markdown}>
      <Markdown remarkPlugins={[remarkGfm]} components={COMPONENTS}>
        {text}
      </Markdown>
    </div>
  );
}
