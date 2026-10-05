import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { Link } from '@skbkontur/react-ui';

import type { SqlCheck } from '../../api/types';
import { useCopy } from '../../hooks/useCopy';
import { texts } from '../../texts';
import { Notice } from '../Notice';
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

type SqlBlockCheck = SqlCheck['blocks'][number];

const NEAR_MAX = 60;
const LINE_HEIGHT_PX = 22;
const PADDING_PX = 12;

/** Итог проверки блока `sql` (концепция §5.7): синтаксис, место ошибки, опасные операции. */
function SqlCheckResult({ check }: { check: SqlBlockCheck }) {
  const t = texts.sql;
  const near = check.error?.near.slice(0, NEAR_MAX);
  return (
    <div className={styles.check}>
      {check.valid === true && (
        <p className={styles.valid}>
          <IconCheckARegular16 aria-hidden="true" />
          {t.check.valid}
        </p>
      )}
      {check.valid === null && (
        <p className={styles.valid}>{t.check.unchecked[check.unchecked ?? ''] ?? t.check.uncheckedOther}</p>
      )}
      {check.error && (
        <Notice kind="warning">
          {t.check.invalid(check.error.line, check.error.column)}
          {near && (
            <>
              {t.check.near}„<span className="p-mono">{near}</span>“
            </>
          )}
          {t.check.beforeRun}
        </Notice>
      )}
      {check.dangers.map((danger) => (
        <Notice key={danger} kind="warning">
          {t.dangers[danger]}
        </Notice>
      ))}
    </div>
  );
}

/**
 * Итог записи, для которой блока на её строке не нашлось (концепция §5.7): предупреждение не теряется,
 * но показывается под текстом ответа и без места ошибки — его не к чему отнести.
 */
export function SqlOrphanNotice({ check }: { check: SqlBlockCheck }) {
  const t = texts.sql;
  const near = check.error?.near.slice(0, NEAR_MAX);
  if (check.dangers.length === 0 && check.valid !== false) {
    return null;
  }
  return (
    <div className={styles.check}>
      <Notice kind="warning">
        {t.check.orphan}
        {check.dangers.map((danger) => `. ${t.dangers[danger]}`)}
        {check.valid === false &&
          (near ? (
            <>
              . {t.check.orphanErrorNear}„<span className="p-mono">{near}</span>“{t.check.beforeRun}
            </>
          ) : (
            `. ${t.check.orphanError}`
          ))}
      </Notice>
    </div>
  );
}

interface CodeBlockProps {
  code: string;
  language: string | null;
  /** Итог проверки — только у блока `sql` в ответе SQL-помощника. */
  check?: SqlBlockCheck;
}

/** Блок кода (концепция §4.2): шапка с названием языка и «Скопировать», длинные строки не переносятся. */
export function CodeBlock({ code, language, check }: CodeBlockProps) {
  const copy = useCopy();
  const { title, nodes } = highlight(code, language);
  const errorLine = check?.error?.line;
  return (
    <>
    <div className={styles.block}>
      <div className={styles.header}>
        <span>{title}</span>
        <Link component="button" onClick={() => void copy.copy(code)}>
          {copy.label}
        </Link>
      </div>
      <pre className={styles.code} tabIndex={0}>
        {/* Строка с ошибкой отмечена полосой слева. */}
        {errorLine !== undefined && (
          <span
            className={styles.errorLine}
            style={{ top: PADDING_PX + (errorLine - 1) * LINE_HEIGHT_PX }}
            data-testid="sql-error-line"
            data-line={errorLine}
          />
        )}
        <code>{nodes ? nodes.map(renderNode) : code}</code>
      </pre>
    </div>
    {check && <SqlCheckResult check={check} />}
    </>
  );
}
