import { Link } from '@skbkontur/react-ui';

import { useCopy } from '../../hooks/useCopy';
import styles from './CodeBlock.module.css';
import { highlight, type HighlightNode } from './highlight';

function renderNode(node: HighlightNode, key: number): React.ReactNode {
  if (node.type === 'text') {
    return node.value;
  }
  if (node.type !== 'element') {
    return null;
  }
  const classes = node.properties.className;
  return (
    <span key={key} className={Array.isArray(classes) ? classes.join(' ') : undefined}>
      {node.children.map(renderNode)}
    </span>
  );
}

/** Блок кода (концепция §4.2): шапка с названием языка и «Скопировать», длинные строки не переносятся. */
export function CodeBlock({ code, language }: { code: string; language: string | null }) {
  const copy = useCopy();
  const { title, nodes } = highlight(code, language);
  return (
    <div className={styles.block}>
      <div className={styles.header}>
        <span>{title}</span>
        <Link component="button" onClick={() => void copy.copy(code)}>
          {copy.label}
        </Link>
      </div>
      <pre className={styles.code} tabIndex={0}>
        <code>{nodes ? nodes.map(renderNode) : code}</code>
      </pre>
    </div>
  );
}
