import { IconPlusRegular16 } from '@skbkontur/icons/IconPlusRegular16';
import { Button, Hint, Kebab, Link, Loader, MenuItem, ScrollContainer } from '@skbkontur/react-ui';
import { NavLink } from 'react-router-dom';

import type { Dialog } from '../../api/types';
import { historyGroup, type ChatState, type HistoryGroup } from '../../chat/state';
import { texts } from '../../texts';
import { Notice } from '../Notice';
import styles from './HistoryPanel.module.css';

const t = texts.chat;
const GROUPS: HistoryGroup[] = ['today', 'yesterday', 'week', 'earlier'];

interface HistoryPanelProps {
  history: ChatState['history'];
  onNewChat: () => void;
  onRename: (dialog: Dialog) => void;
  onRemove: (dialog: Dialog) => void;
  onShowMore: () => void;
  onRetry: () => void;
}

function rowClass({ isActive }: { isActive: boolean }): string {
  return isActive ? `${styles.link} ${styles.current}` : (styles.link ?? '');
}

/** Панель раздела — история чатов по дате последнего сообщения (концепция §5.5). */
export function HistoryPanel({ history, onNewChat, onRename, onRemove, onShowMore, onRetry }: HistoryPanelProps) {
  const now = new Date();
  const groups = GROUPS.map((group) => ({
    group,
    items: history.items.filter((dialog) => historyGroup(dialog.updated_at, now) === group),
  })).filter(({ items }) => items.length > 0);

  return (
    <aside className={styles.panel} aria-label={t.history}>
      <div className={styles.new}>
        <Button use="primary" width="100%" icon={<IconPlusRegular16 />} onClick={onNewChat}>
          {t.newChat}
        </Button>
      </div>
      <div className={styles.scroll}>
        <ScrollContainer>
          <Loader
            active={history.status === 'loading' && history.items.length === 0}
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
                      const title = dialog.title ?? t.newChat;
                      return (
                        <li key={dialog.id} className={styles.item} data-opener={`history-${dialog.id}`}>
                          <Hint text={title} pos="right">
                            <NavLink to={`/chat/${dialog.id}`} className={rowClass}>
                              {title}
                            </NavLink>
                          </Hint>
                          <span className={styles.kebab}>
                            <Hint text={texts.users.actions}>
                              <Kebab aria-label={`${texts.users.actions}: ${title}`}>
                                <MenuItem onClick={() => onRename(dialog)}>{t.menu.rename}</MenuItem>
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
