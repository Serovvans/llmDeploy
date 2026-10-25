import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { IconPlusRegular16 } from '@skbkontur/icons/IconPlusRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Input, Kebab, Link, MenuItem, ScrollContainer, SingleToast } from '@skbkontur/react-ui';
import { Fragment, useId, useMemo, useRef, useState } from 'react';

import type { CodeConfig, CodeFile } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { TitleHint } from '../components/TitleHint';
import { texts } from '../texts';
import styles from './FileTree.module.css';
import { buildTree, filePathError, splitPath, type TreeNode } from './rules';

const t = texts.workshop.files;

interface Row {
  key: string;
  level: number;
  node: TreeNode;
  parent: string | null;
}

/** Видимые строки дерева сверху вниз: свёрнутая папка прячет своё содержимое. */
function visibleRows(nodes: TreeNode[], collapsed: ReadonlySet<string>, level = 1, parent: string | null = null): Row[] {
  return nodes.flatMap((node) => {
    const key = node.kind === 'folder' ? `d:${node.path}` : `f:${node.file.id}`;
    const row: Row = { key, level, node, parent };
    if (node.kind === 'folder' && !collapsed.has(node.path)) {
      return [row, ...visibleRows(node.children, collapsed, level + 1, key)];
    }
    return [row];
  });
}

interface NameFieldProps {
  initial: string;
  files: readonly CodeFile[];
  config: CodeConfig;
  self?: string;
  level: number;
  /** Ошибка сервера — текст под полем; файл — готово. */
  onSave: (path: string) => Promise<CodeFile | string>;
  onDone: (file: CodeFile | null) => void;
}

/** Поле имени прямо в дереве: Enter сохраняет, Esc отменяет, потеря фокуса сохраняет (§5.13.5). */
function NameField({ initial, files, config, self, level, onSave, onDone }: NameFieldProps) {
  const [value, setValue] = useState(initial);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const finished = useRef(false);
  const errorId = useId();

  const save = async (fromBlur: boolean) => {
    if (finished.current || pending) {
      return;
    }
    const path = value.trim();
    if (path === initial && self !== undefined) {
      finished.current = true;
      onDone(null);
      return;
    }
    if (!path && fromBlur) {
      finished.current = true;
      onDone(null);
      return;
    }
    const problem = filePathError(path, files, config, self);
    if (problem) {
      // Ошибка видна справа от поля; потеря фокуса с ошибкой поле не закрывает — иначе текст пропал бы.
      setError(problem);
      return;
    }
    setPending(true);
    const result = await onSave(path);
    setPending(false);
    if (typeof result === 'string') {
      if (result) {
        setError(result);
      } else {
        finished.current = true;
        onDone(null);
      }
      return;
    }
    finished.current = true;
    onDone(result);
  };

  return (
    <div className={styles.field} style={{ paddingLeft: 8 + (level - 1) * 16 }}>
      <Input
        size="small"
        width="100%"
        autoFocus
        selectAllOnFocus={self !== undefined}
        aria-label={t.label}
        aria-describedby={error ? errorId : undefined}
        placeholder={t.placeholder}
        className={styles.input}
        error={error !== null}
        disabled={pending}
        value={value}
        onValueChange={(next) => {
          setValue(next);
          setError(null);
        }}
        onKeyDown={(event) => {
          if (event.key === 'Enter') {
            event.preventDefault();
            void save(false);
          } else if (event.key === 'Escape') {
            event.preventDefault();
            finished.current = true;
            onDone(null);
          }
        }}
        onBlur={() => void save(true)}
      />
      {error && (
        <span id={errorId} className={styles.error} role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

interface ProjectFilesProps {
  files: readonly CodeFile[];
  config: CodeConfig;
  activeFileId: string | null;
  /** Ошибок последней сборки по пути файла. */
  errors: ReadonlyMap<string, number>;
  onOpen: (fileId: string) => void;
  create: (path: string) => Promise<CodeFile | string>;
  rename: (fileId: string, path: string) => Promise<CodeFile | string>;
  remove: (fileId: string) => Promise<boolean>;
  /** Файл удалён: вкладка закрывается. */
  onRemoved: (fileId: string) => void;
}

/** «Файлы», «+ Файл» и дерево проекта (концепция §5.13.5). */
export function ProjectFiles(props: ProjectFilesProps) {
  const { files, config, activeFileId, errors } = props;
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set());
  const [editing, setEditing] = useState<'new' | string | null>(null);
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [removing, setRemoving] = useState<CodeFile | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const treeRef = useRef<HTMLUListElement>(null);
  const rows = useMemo(() => visibleRows(buildTree(files), collapsed), [collapsed, files]);
  const activeKey = activeFileId ? `f:${activeFileId}` : null;
  // В обходе клавишей Tab — одна строка дерева: последняя с фокусом, иначе текущий файл, иначе первая.
  const tabKey = rows.find((row) => row.key === focusKey)?.key ?? rows.find((row) => row.key === activeKey)?.key ?? rows[0]?.key;

  const focusRow = (key: string | undefined) => {
    if (!key) {
      return;
    }
    setFocusKey(key);
    treeRef.current?.querySelector<HTMLElement>(`[data-row="${CSS.escape(key)}"]`)?.focus();
  };

  const toggle = (path: string, open?: boolean) =>
    setCollapsed((current) => {
      const next = new Set(current);
      const shouldOpen = open ?? next.has(path);
      if (shouldOpen) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });

  const onKeyDown = (event: React.KeyboardEvent, row: Row, index: number) => {
    const { node } = row;
    switch (event.key) {
      case 'ArrowDown':
        focusRow(rows[index + 1]?.key);
        break;
      case 'ArrowUp':
        focusRow(rows[index - 1]?.key);
        break;
      case 'Home':
        focusRow(rows[0]?.key);
        break;
      case 'End':
        focusRow(rows.at(-1)?.key);
        break;
      case 'ArrowRight':
        if (node.kind === 'folder') {
          if (collapsed.has(node.path)) {
            toggle(node.path, true);
          } else {
            focusRow(rows[index + 1]?.key);
          }
        }
        break;
      case 'ArrowLeft':
        if (node.kind === 'folder' && !collapsed.has(node.path)) {
          toggle(node.path, false);
        } else {
          focusRow(row.parent ?? undefined);
        }
        break;
      case 'Enter':
      case ' ':
        if (node.kind === 'folder') {
          toggle(node.path);
        } else {
          props.onOpen(node.file.id);
        }
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  const confirmRemove = async () => {
    if (!removing) {
      return;
    }
    const index = rows.findIndex((row) => row.key === `f:${removing.id}`);
    setRemovePending(true);
    const done = await props.remove(removing.id);
    setRemovePending(false);
    const removed = removing;
    setRemoving(null);
    if (done) {
      SingleToast.push(t.removed);
      props.onRemoved(removed.id);
      // Фокус — на соседний файл дерева (§5.13.5).
      const neighbour = rows.slice(index + 1).find((row) => row.node.kind === 'file') ?? rows.slice(0, index).reverse().find((row) => row.node.kind === 'file');
      window.setTimeout(() => focusRow(neighbour?.key), 0);
    }
  };

  const nameField = (level: number, self?: CodeFile) => (
    <li role="none">
      <NameField
        initial={self?.path ?? ''}
        files={files}
        config={config}
        self={self?.id}
        level={level}
        onSave={(path) => (self ? props.rename(self.id, path) : props.create(path))}
        onDone={(file) => {
          setEditing(null);
          if (file) {
            if (!self) {
              props.onOpen(file.id);
            }
            setFocusKey(`f:${file.id}`);
          }
        }}
      />
    </li>
  );

  return (
    <>
      <div className={styles.head}>
        <h2 className={styles.title}>{t.title}</h2>
        <Button size="small" icon={<IconPlusRegular16 />} onClick={() => setEditing('new')}>
          {t.add}
        </Button>
      </div>
      <div className={styles.scroll}>
        <ScrollContainer>
          {files.length === 0 && editing !== 'new' ? (
            <div className={styles.empty}>
              <p>{t.empty}</p>
              <Link component="button" onClick={() => setEditing('new')}>{t.addFirst}</Link>
            </div>
          ) : (
            <ul ref={treeRef} role="tree" aria-label={t.title} className={styles.tree}>
              {editing === 'new' && nameField(1)}
              {rows.map((row, index) => {
                const { node } = row;
                if (node.kind === 'file' && editing === node.file.id) {
                  return <Fragment key={row.key}>{nameField(row.level, node.file)}</Fragment>;
                }
                const errorCount = node.kind === 'file' ? (errors.get(node.file.path) ?? 0) : 0;
                const current = row.key === activeKey;
                return (
                  <li key={row.key} role="none" className={styles.item}>
                    <div
                      role="treeitem"
                      aria-level={row.level}
                      aria-expanded={node.kind === 'folder' ? !collapsed.has(node.path) : undefined}
                      aria-selected={node.kind === 'file' ? current : undefined}
                      tabIndex={row.key === tabKey ? 0 : -1}
                      data-row={row.key}
                      className={current ? `${styles.row} ${styles.current}` : styles.row}
                      style={{ paddingLeft: 8 + (row.level - 1) * 16 }}
                      onFocus={() => setFocusKey(row.key)}
                      onKeyDown={(event) => onKeyDown(event, row, index)}
                      onClick={() => (node.kind === 'folder' ? toggle(node.path) : props.onOpen(node.file.id))}
                    >
                      {node.kind === 'folder' &&
                        (collapsed.has(node.path) ? <IconArrowCRightRegular16 /> : <IconArrowCDownRegular16 />)}
                      <TitleHint text={node.kind === 'folder' ? node.path : node.file.path} className={styles.name}>
                        <span className={styles.nameText}>{node.name}</span>
                      </TitleHint>
                      {errorCount > 0 && (
                        <span className={styles.badge} aria-label={t.errorsLabel(errorCount)} role="img">
                          {errorCount}
                          <IconXCircleRegular16 />
                        </span>
                      )}
                    </div>
                    {node.kind === 'file' && (
                      <span className={styles.kebab}>
                        <Kebab size="small" aria-label={`${texts.workshop.actions}: ${node.name}`}>
                          <MenuItem onClick={() => setEditing(node.file.id)}>{t.rename}</MenuItem>
                          <MenuItem onClick={() => setRemoving(node.file)}>{t.remove}</MenuItem>
                        </Kebab>
                      </span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </ScrollContainer>
      </div>
      {removing && (
        <ConfirmModal
          title={t.removeTitle(splitPath(removing.path).name)}
          body={t.removeBody}
          action={t.removeAction}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </>
  );
}
