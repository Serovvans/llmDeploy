import { Button, Checkbox, Link, Loader } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import type { CodeEditFull } from '../api/types';
import { Notice } from '../components/Notice';
import { texts } from '../texts';
import type { ResolvedTheme } from '../theme/preference';
import { DiffView } from './DiffView';
import { loadEdit } from './EditProposal';
import styles from './EditTab.module.css';
import { lineDiffStats } from './rules';

const e = texts.workshop.edit;
type Setup = typeof import('./monaco/setup');

/** Итог решения по правке — от экрана проекта, он же меняет файлы и ленту. */
export type Decision =
  | { kind: 'done' }
  | { kind: 'decided' }
  | { kind: 'outdated'; paths: string[] }
  | { kind: 'failed' };

interface Props {
  setup: Setup | null;
  theme: ResolvedTheme;
  projectId: string;
  messageId: string;
  decide: (accept: { path: string; revision: number | null }[], total: number) => Promise<Decision>;
  /** Вкладку открыли «Посмотреть и принять»: фокус — на первый `Checkbox` файла (§9). */
  focusFirst: boolean;
  onFocused: () => void;
}

type Loaded = { status: 'loading' } | { status: 'failed' } | { status: 'ready'; edit: CodeEditFull };

/** Вкладка «Правка помощника» (концепция §5.13.11): разница по файлам, выбор файлов, решение. */
export function EditTab({ setup, theme, projectId, messageId, decide, focusFirst, onFocused }: Props) {
  const [loaded, setLoaded] = useState<Loaded>({ status: 'loading' });
  const [selected, setSelected] = useState<string | null>(null);
  const [unchecked, setUnchecked] = useState<ReadonlySet<string>>(new Set());
  const [chooseError, setChooseError] = useState(false);
  const [pending, setPending] = useState<'accept' | 'reject' | null>(null);
  const [notice, setNotice] = useState<{ kind: 'warning' | 'error'; text: string } | null>(null);
  const root = useRef<HTMLDivElement>(null);

  const load = useCallback(
    (fresh: boolean) =>
      loadEdit(projectId, messageId, fresh).then(
        (edit) => {
          setLoaded({ status: 'ready', edit });
          return edit;
        },
        () => {
          setLoaded({ status: 'failed' });
          return null;
        },
      ),
    [messageId, projectId],
  );

  // Текущие файлы (`current`) нужны свежими: разницу всегда читает заново.
  useEffect(() => {
    void load(true);
  }, [load]);

  useEffect(() => {
    if (focusFirst && loaded.status === 'ready') {
      root.current?.querySelector<HTMLElement>('input[type="checkbox"]')?.focus();
      onFocused();
    }
  }, [focusFirst, loaded.status, onFocused]);

  if (loaded.status === 'loading') {
    return (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  }
  if (loaded.status === 'failed') {
    return (
      <div className={styles.failed}>
        <Notice kind="error" action={{ label: texts.common.retry, onClick: () => void load(true) }}>
          {e.loadFailed}
        </Notice>
      </div>
    );
  }

  const { edit } = loaded;
  const files = edit.files;
  const current = files.find((file) => file.path === selected) ?? files[0];
  const checked = files.filter((file) => !unchecked.has(file.path));
  const open = edit.status === 'proposed';

  const run = async (kind: 'accept' | 'reject') => {
    if (kind === 'accept' && checked.length === 0) {
      setChooseError(true);
      return;
    }
    setPending(kind);
    setNotice(null);
    const accept = kind === 'accept' ? checked.map((file) => ({ path: file.path, revision: file.current?.revision ?? null })) : [];
    const result = await decide(accept, files.length);
    setPending(null);
    if (result.kind === 'decided') {
      const fresh = await load(true);
      setNotice({ kind: 'error', text: fresh?.status === 'superseded' ? e.decidedSuperseded : e.decided });
    } else if (result.kind === 'outdated') {
      await load(true);
      setNotice({ kind: 'warning', text: e.outdated(result.paths.join(', ')) });
    }
  };

  const acceptLabel = checked.length === files.length ? e.acceptAll : e.acceptSome(checked.length, files.length);

  return (
    <div ref={root} className={styles.tab}>
      <div className={styles.head}>
        <h2 className={styles.title}>{e.tabTitle(files.length)}</h2>
        {open && (
          <div className={styles.actions}>
            <div className={styles.accept}>
              <Button use="primary" size="small" loading={pending === 'accept'} disabled={pending === 'reject'} onClick={() => void run('accept')}>
                {acceptLabel}
              </Button>
              {chooseError && (
                <span className={styles.error} role="alert">
                  {e.chooseFiles}
                </span>
              )}
            </div>
            <Link component="button" disabled={pending !== null} onClick={() => void run('reject')}>
              {e.reject}
            </Link>
          </div>
        )}
      </div>
      <div className={styles.files} role="group" aria-label={e.tabTitle(files.length)}>
        {files.map((file) => {
          const stats = lineDiffStats(file.current?.content ?? '', file.after);
          const isCurrent = file === current;
          return (
            <div key={file.path} className={isCurrent ? `${styles.file} ${styles.current}` : styles.file}>
              {open && (
                <Checkbox
                  aria-label={e.acceptFile(file.path)}
                  checked={!unchecked.has(file.path)}
                  disabled={pending !== null}
                  onValueChange={(value) => {
                    setChooseError(false);
                    setUnchecked((set) => {
                      const next = new Set(set);
                      if (value) {
                        next.delete(file.path);
                      } else {
                        next.add(file.path);
                      }
                      return next;
                    });
                  }}
                />
              )}
              <button type="button" className={styles.fileButton} aria-pressed={isCurrent} onClick={() => setSelected(file.path)}>
                <span className={styles.mono}>{file.path}</span>
                <span className={styles.muted}>{file.change === 'added' ? e.added : e.modified}</span>
                <span className={styles.mono}>
                  {stats.added > 0 && `+${stats.added}`}
                  {stats.removed > 0 && ` −${stats.removed}`}
                </span>
              </button>
            </div>
          );
        })}
      </div>
      {notice && (
        <div className={styles.notice}>
          <Notice kind={notice.kind}>{notice.text}</Notice>
        </div>
      )}
      {current?.outdated && open && (
        <div className={styles.notice}>
          <Notice kind="warning">{e.changedSince(current.path)}</Notice>
        </div>
      )}
      {current && current.current === null && <p className={styles.newFile}>{e.newFile}</p>}
      {current && setup && (
        <DiffView setup={setup} theme={theme} path={current.path} before={current.current?.content ?? ''} after={current.after} />
      )}
    </div>
  );
}
