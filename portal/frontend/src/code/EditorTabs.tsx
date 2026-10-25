import { IconUiCompareRegular16 } from '@skbkontur/icons/IconUiCompareRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Kebab, MenuItem, Spinner } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { texts } from '../texts';
import styles from './EditorTabs.module.css';
import { tabLabel } from './rules';
import type { SaveState } from './useProject';

const t = texts.workshop.editor;
/** Особая вкладка правки помощника — всегда первая (§5.13.6). */
export const EDIT_TAB = 'edit';
/** «Сохраняю…» появляется не сразу: короткое сохранение не мигает (§5.13.6). */
const SAVING_DELAY_MS = 300;

interface Props {
  tabs: readonly { id: string; path: string }[];
  /** Вкладка «Правка помощника»: число файлов; `null` — её нет. */
  edit: number | null;
  active: string | null;
  saveState: SaveState;
  /** Редактор в фокусе после перехода клавиатурой — строка про Ctrl+M (§9). */
  tabHint: boolean;
  onSelect: (id: string) => void;
  onClose: (id: string) => void;
}

function SaveLine({ state }: { state: SaveState }) {
  const [late, setLate] = useState(false);
  const saving = state.kind === 'saving';
  useEffect(() => {
    if (!saving) {
      return;
    }
    const timer = window.setTimeout(() => setLate(true), SAVING_DELAY_MS);
    return () => {
      window.clearTimeout(timer);
      setLate(false);
    };
  }, [saving]);
  switch (state.kind) {
    case 'saving':
      return late ? (
        <span className={styles.save}>
          <Spinner type="mini" caption={t.saving} />
        </span>
      ) : (
        <span className={styles.save}>{t.saved}</span>
      );
    case 'offline':
      return (
        <span className={styles.save}>
          <IconWarningTriangleRegular16 className={styles.warning} />
          {t.offline}
        </span>
      );
    case 'error':
      return <span className={`${styles.save} ${styles.failed}`}>{state.text}</span>;
    default:
      return <span className={styles.save}>{t.saved}</span>;
  }
}

/** Вкладки открытых файлов (концепция §5.13.6, §9): стрелки — по вкладкам, крестик — следом в обходе. */
export function EditorTabs({ tabs, edit, active, saveState, tabHint, onSelect, onClose }: Props) {
  const list = useRef<HTMLDivElement>(null);
  const ids = [...(edit !== null ? [EDIT_TAB] : []), ...tabs.map((tab) => tab.id)];
  const openPaths = tabs.map((tab) => tab.path);

  // Текущая вкладка видна, даже если вкладки не помещаются в строку.
  useEffect(() => {
    list.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }, [active]);

  const move = (event: React.KeyboardEvent, index: number) => {
    const next = event.key === 'ArrowRight' ? index + 1 : event.key === 'ArrowLeft' ? index - 1 : null;
    if (next === null) {
      return;
    }
    event.preventDefault();
    const id = ids[(next + ids.length) % ids.length];
    list.current?.querySelector<HTMLElement>(`[data-tab="${CSS.escape(id ?? '')}"]`)?.focus();
  };

  const label = (id: string) => {
    if (id === EDIT_TAB) {
      return { name: t.editTab, folder: null };
    }
    return tabLabel(tabs.find((tab) => tab.id === id)?.path ?? '', openPaths);
  };

  return (
    <div className={styles.bar}>
      <div ref={list} role="tablist" aria-label={t.openTabs} className={styles.tabs}>
        {ids.map((id, index) => {
          const { name, folder } = label(id);
          const selected = id === active;
          return (
            <div key={id} className={selected ? `${styles.tab} ${styles.selected}` : styles.tab}>
              <button
                type="button"
                role="tab"
                data-tab={id}
                aria-selected={selected}
                tabIndex={selected || (active === null && index === 0) ? 0 : -1}
                className={id === EDIT_TAB ? `${styles.tabButton} ${styles.editTab}` : styles.tabButton}
                onClick={() => onSelect(id)}
                onKeyDown={(event) => move(event, index)}
              >
                {id === EDIT_TAB && <IconUiCompareRegular16 />}
                <span className={styles.tabName}>{name}</span>
                {folder && <span className={styles.folder}>· {folder}</span>}
              </button>
              <button
                type="button"
                className={styles.close}
                aria-label={id === EDIT_TAB ? texts.workshop.edit.closeTab : t.closeTab(name)}
                onClick={() => onClose(id)}
              >
                <IconXRegular16 />
              </button>
            </div>
          );
        })}
      </div>
      <div className={styles.side}>
        {tabHint ? <span className={styles.save}>{t.tabHint}</span> : <SaveLine state={saveState} />}
        {ids.length > 0 && (
          <Kebab size="small" aria-label={t.openTabs}>
            {ids.map((id) => {
              const { name, folder } = label(id);
              return (
                <MenuItem key={id} onClick={() => onSelect(id)}>
                  {folder ? `${name} · ${folder}` : name}
                </MenuItem>
              );
            })}
          </Kebab>
        )}
      </div>
    </div>
  );
}
