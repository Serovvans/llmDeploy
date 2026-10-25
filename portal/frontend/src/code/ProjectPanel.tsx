import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconPlusRegular16 } from '@skbkontur/icons/IconPlusRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Hint, Kebab, Link, Loader, MenuItem, MenuSeparator, ScrollContainer, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useId, useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';

import type { Dialog } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { exportDocx } from '../chat/exportDocx';
import { ConfirmModal } from '../components/ConfirmModal';
import { Notice } from '../components/Notice';
import { TitleHint } from '../components/TitleHint';
import { useMemoryUi } from '../memory/useMemoryUi';
import { formatDate, texts } from '../texts';
import { useCode } from './CodeContext';
import { useProjectActions } from './projectActions';
import styles from './ProjectPanel.module.css';
import { listBadge } from './rules';

const w = texts.workshop;

function rowClass({ isActive }: { isActive: boolean }): string {
  return isActive ? `${styles.link} ${styles.current}` : (styles.link ?? '');
}

/** Прежние диалоги помощника CoGIS — свёрнутая группа, только для чтения (§5.13.3, §5.13.14). */
function OldDialogs() {
  const cogis = useDialogs('cogis');
  const { history, loadHistory } = cogis;
  // Открыт прежний диалог — группа развёрнута, чтобы была видна его строка.
  const [open, setOpen] = useState(() => window.location.pathname.startsWith('/code/dialogs/'));
  const [removing, setRemoving] = useState<Dialog | null>(null);
  const [pending, setPending] = useState(false);
  const listId = useId();
  const labels = texts.dialogs.cogis;
  // Окно переноса — то же, что у диалога; «Куда» по умолчанию — «Код» (§4.9).
  const memory = useMemoryUi({
    kind: 'cogis',
    dialogId: null,
    title: null,
    view: undefined,
    available: false,
    params: { action: 'write' },
    busy: false,
  });

  const idle = history.status === 'idle';
  useEffect(() => {
    if (idle) {
      loadHistory(false);
    }
  }, [idle, loadHistory]);

  if (history.items.length === 0) {
    return memory.dialogs;
  }

  const confirm = async () => {
    if (!removing) {
      return;
    }
    setPending(true);
    try {
      await cogis.remove(removing.id);
      SingleToast.push(labels.removed);
    } catch {
      SingleToast.push(labels.removeFailed, { use: 'error' });
    } finally {
      setPending(false);
      setRemoving(null);
    }
  };

  return (
    <section className={styles.old}>
      <Button
        use="text"
        icon={open ? <IconArrowCDownRegular16 /> : <IconArrowCRightRegular16 />}
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen(!open)}
      >
        <span className={styles.oldTitle}>{w.oldDialogs}</span>
      </Button>
      {open && (
        <div id={listId}>
          <p className={styles.muted}>{w.readOnly}</p>
          <ul className={styles.items}>
            {history.items.map((dialog) => {
              const title = dialog.title ?? labels.newLabel;
              return (
                <li key={dialog.id} className={styles.item} data-opener={`old-${dialog.id}`}>
                  <TitleHint text={title} pos="right">
                    <NavLink to={`/code/dialogs/${dialog.id}`} className={rowClass}>
                      <span className={styles.name}>{title}</span>
                    </NavLink>
                  </TitleHint>
                  <span className={styles.kebab}>
                    <Hint text={texts.users.actions}>
                      <Kebab aria-label={`${texts.users.actions}: ${title}`}>
                        <MenuItem onClick={() => void exportDocx(dialog.id)}>{texts.chat.menu.exportDocx}</MenuItem>
                        {memory.transfer && (
                          <MenuItem
                            onClick={() => memory.transfer?.(dialog, `[data-opener="old-${dialog.id}"] [tabindex="0"]`)}
                          >
                            {texts.memory.transfer.menu}
                          </MenuItem>
                        )}
                        <MenuItem onClick={() => setRemoving(dialog)}>{texts.chat.menu.remove}</MenuItem>
                      </Kebab>
                    </Hint>
                  </span>
                </li>
              );
            })}
          </ul>
          {history.nextCursor && (
            <p className={styles.more}>
              <Link component="button" onClick={() => loadHistory(true)}>
                {w.showMore}
              </Link>
            </p>
          )}
        </div>
      )}
      {memory.dialogs}
      {removing && (
        <ConfirmModal
          title={labels.removeTitle(removing.title ?? labels.newLabel)}
          body={labels.removeBody}
          action={texts.chat.remove.action}
          pending={pending}
          onConfirm={() => void confirm()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </section>
  );
}

/** Панель раздела «Код» в режиме списка (свой: `ProjectPanel`, §5.13.3). */
export function ProjectList() {
  const navigate = useNavigate();
  const { projects, loadProjects } = useCode();
  const actions = useProjectActions();
  const now = new Date();

  return (
    <aside className={styles.panel} aria-label={w.projects}>
      <div className={styles.new}>
        <Button use="primary" width="100%" icon={<IconPlusRegular16 />} onClick={() => navigate('/code/new')}>
          {w.newTool}
        </Button>
      </div>
      <div className={styles.scroll}>
        <ScrollContainer>
          <Loader
            active={projects.status === 'loading' && projects.items.length === 0}
            caption={texts.common.loading}
            delayBeforeSpinnerShow={300}
          >
            <div className={styles.groups}>
              <section>
                <h2 className={styles.group}>{w.projects}</h2>
                {projects.status === 'failed' && (
                  <Notice kind="error" action={{ label: texts.common.retry, onClick: () => loadProjects(false) }}>
                    {w.listFailed}
                  </Notice>
                )}
                {projects.status === 'ready' && projects.items.length === 0 && <p className={styles.muted}>{w.noProjects}</p>}
                <ul className={styles.items}>
                  {projects.items.map((project) => {
                    const badge = listBadge(project.build);
                    return (
                      <li key={project.id} className={styles.item}>
                        <TitleHint text={project.name} pos="right">
                          <NavLink to={`/code/${project.id}`} className={rowClass}>
                            <span className={styles.name}>{project.name}</span>
                            <span className={styles.time}>{formatDate(project.updated_at, now)}</span>
                          </NavLink>
                        </TitleHint>
                        {badge && (
                          <span
                            className={badge === 'ok' ? styles.badge : `${styles.badge} ${styles.badgeError}`}
                            role="img"
                            aria-label={w.buildBadge[badge]}
                          >
                            {badge === 'ok' ? <IconCheckARegular16 /> : <IconXCircleRegular16 />}
                          </span>
                        )}
                        <span className={styles.kebab}>
                          <Hint text={texts.users.actions}>
                            <Kebab aria-label={`${texts.users.actions}: ${project.name}`}>
                              <MenuItem onClick={() => actions.exportProject(project.id, project.name)}>{w.exportVs}</MenuItem>
                              <MenuSeparator />
                              <MenuItem onClick={() => actions.askRemove(project)}>{w.removeProject}</MenuItem>
                            </Kebab>
                          </Hint>
                        </span>
                      </li>
                    );
                  })}
                </ul>
                {projects.nextCursor && (
                  <p className={styles.more}>
                    <Link component="button" onClick={() => loadProjects(true)}>
                      {w.showMore}
                    </Link>
                  </p>
                )}
              </section>
              <OldDialogs />
            </div>
          </Loader>
        </ScrollContainer>
      </div>
      {actions.modal}
    </aside>
  );
}
