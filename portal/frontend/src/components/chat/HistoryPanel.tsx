import { IconPlusRegular16 } from '@skbkontur/icons/IconPlusRegular16';
import { Button, Hint, Kebab, Link, Loader, MenuItem, ScrollContainer } from '@skbkontur/react-ui';
import { NavLink } from 'react-router-dom';

import type { Dialog } from '../../api/types';
import { historyGroup, type History, type HistoryGroup } from '../../chat/state';
import { texts } from '../../texts';
import { Notice } from '../Notice';
import { TitleHint } from '../TitleHint';
import styles from './HistoryPanel.module.css';

const t = texts.chat;
const GROUPS: HistoryGroup[] = ['today', 'yesterday', 'week', 'earlier'];

interface HistoryPanelProps {
  history: History;
  /** Адрес раздела: строка истории ведёт на `basePath/:id`. */
  basePath: string;
  /** Подпись кнопки и название диалога, которому модель ещё не дала названия. */
  newLabel: string;
  onNewChat: () => void;
  /** «Переименовать» есть не у всех разделов: название разбора — имя файла. */
  onRename?: (dialog: Dialog) => void;
  onExport: (dialog: Dialog) => void;
  onRemove: (dialog: Dialog) => void;
  onShowMore: () => void;
  onRetry: () => void;
}

function rowClass({ isActive }: { isActive: boolean }): string {
  return isActive ? `${styles.link} ${styles.current}` : (styles.link ?? '');
}

/** Панель раздела — история чатов по дате последнего сообщения (концепция §5.5). */
export function HistoryPanel(props: HistoryPanelProps) {
  const { history, basePath, newLabel, onNewChat, onRename, onExport, onRemove, onShowMore, onRetry } = props;
  const now = new Date();
  const groups = GROUPS.map((group) => ({
    group,
    items: history.items.filter((dialog) => historyGroup(dialog.updated_at, now) === group),
  })).filter(({ items }) => items.length > 0);

  return (
    <aside className={styles.panel} aria-label={t.history}>
      <div className={styles.new}>
        <Button use="primary" width="100%" icon={<IconPlusRegular16 />} onClick={onNewChat}>
          {newLabel}
        </Button>
      </div>
      <div className={styles.scroll}>
        <ScrollContainer>
          <Loader
            active={history.status !== 'ready' && history.status !== 'failed' && history.items.length === 0}
            caption={texts.common.loading}
            delayBeforeSpinnerShow={300}
          >
            <div className={styles.groups}>
              {history.status === 'failed' && (
                <Notice kind="error" action={{ label: texts.common.retry, onClick: onRetry }}>
                  {texts.common.actionFailed}
                </Notice>
              )}
              {groups.map(({ group, items }) => (
                <section key={group}>
                  <h2 className={styles.group}>{t.groups[group]}</h2>
                  <ul className={styles.items}>
                    {items.map((dialog) => {
                      const title = dialog.title ?? newLabel;
                      return (
                        <li key={dialog.id} className={styles.item} data-opener={`history-${dialog.id}`}>
                          <TitleHint text={title} pos="right">
                            <NavLink to={`${basePath}/${dialog.id}`} className={rowClass}>
                              {title}
                            </NavLink>
                          </TitleHint>
                          <span className={styles.kebab}>
                            <Hint text={texts.users.actions}>
                              <Kebab aria-label={`${texts.users.actions}: ${title}`}>
                                {onRename && <MenuItem onClick={() => onRename(dialog)}>{t.menu.rename}</MenuItem>}
                                <MenuItem onClick={() => onExport(dialog)}>{t.menu.exportDocx}</MenuItem>
                                <MenuItem onClick={() => onRemove(dialog)}>{t.menu.remove}</MenuItem>
                              </Kebab>
                            </Hint>
                          </span>
                        </li>
                      );
                    })}
                  </ul>
                </section>
              ))}
              {history.nextCursor && (
                <p className={styles.more}>
                  <Link component="button" onClick={onShowMore}>
                    {t.showMore}
                  </Link>
                </p>
              )}
            </div>
          </Loader>
        </ScrollContainer>
      </div>
    </aside>
  );
}
