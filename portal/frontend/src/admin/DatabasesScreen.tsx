import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Hint, Kebab, Link, Loader, MenuItem, MenuSeparator, ScrollContainer, SingleToast, Spinner } from '@skbkontur/react-ui';
import { useCallback, useEffect, useState } from 'react';
import { Navigate, useNavigate } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { AdminDbConnection, SortOrder } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { DataTable, SortableHeader } from '../components/DataTable';
import { EmptyState } from '../components/EmptyState';
import { Notice } from '../components/Notice';
import { TitleHint } from '../components/TitleHint';
import { ConnectionForm } from '../db/ConnectionForm';
import { dbRefusal } from '../db/refusal';
import { excessCount } from '../db/rights';
import { snapshotDay } from '../db/selection';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { formatDate, texts } from '../texts';
import { AdminHeader } from './AdminHeader';
import styles from './DatabasesScreen.module.css';

const a = texts.db.admin;

type Form = { connection: AdminDbConnection | null; focusCheck: boolean } | null;

/** Базы данных — `/admin/databases` (концепция §5.14): только администратор и только при включённых подключениях. */
export function DatabasesScreen() {
  usePageTitle(a.title);
  const { session, config } = useSession();
  const navigate = useNavigate();
  if (session?.user?.role !== 'admin') {
    return (
      <EmptyState
        headingLevel="h1"
        title={texts.users.noAccess.title}
        text={texts.users.noAccess.text}
        action={{ label: texts.common.goToChat, onClick: () => navigate('/chat') }}
      />
    );
  }
  if (!config) {
    return null;
  }
  if (!config.db.enabled) {
    return <Navigate to="/admin/users" replace />;
  }
  return <Databases />;
}

function Databases() {
  const { config, reloadConfig } = useSession();
  const navigate = useNavigate();
  const [items, setItems] = useState<AdminDbConnection[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [reloads, setReloads] = useState(0);
  const [order, setOrder] = useState<SortOrder>('asc');
  const [form, setForm] = useState<Form>(null);
  const [removing, setRemoving] = useState<AdminDbConnection | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const [checking, setChecking] = useState<ReadonlySet<string>>(new Set());
  const rememberOpener = useFocusReturn(form !== null || removing !== null);

  /** Подключения выключили, пока экран открыт (§5.14): переход к пользователям. */
  const featureOff = useCallback(() => {
    reloadConfig();
    SingleToast.push(texts.db.refusals.off);
    navigate('/admin/users', { replace: true });
  }, [navigate, reloadConfig]);

  useEffect(() => {
    let cancelled = false;
    api.adminListDbConnections().then(
      ({ items: loaded }) => {
        if (!cancelled) {
          setItems(loaded);
          setFailed(false);
        }
      },
      (error: unknown) => {
        if (cancelled) {
          return;
        }
        if (isApiError(error, 'feature_disabled')) {
          featureOff();
        } else {
          setFailed(true);
        }
      },
    );
    return () => {
      cancelled = true;
    };
  }, [featureOff, reloads]);

  const reload = useCallback(() => setReloads((value) => value + 1), []);
  const replace = (next: AdminDbConnection) =>
    setItems((current) => current && current.map((item) => (item.id === next.id ? next : item)));

  const openForm = (next: NonNullable<Form>, opener: string) => {
    rememberOpener(opener);
    setForm(next);
  };

  const check = async (connection: AdminDbConnection) => {
    setChecking((current) => new Set(current).add(connection.id));
    try {
      replace(await api.adminCheckDbConnection(connection.id));
    } catch (error) {
      refused(error);
    } finally {
      setChecking((current) => {
        const next = new Set(current);
        next.delete(connection.id);
        return next;
      });
    }
  };

  const refresh = async (connection: AdminDbConnection) => {
    try {
      const snapshot = await api.refreshDbSnapshot(connection.id);
      SingleToast.push(texts.db.picker.refreshed(snapshot.tables.length));
      reload();
    } catch (error) {
      if (isApiError(error, 'db_snapshot_too_soon')) {
        SingleToast.push(error.message);
      } else {
        refused(error);
      }
    }
  };

  const refused = (error: unknown) => {
    if (isApiError(error, 'feature_disabled')) {
      featureOff();
    } else if (isApiError(error, 'not_found')) {
      SingleToast.push(a.alreadyRemoved);
      reload();
    } else if (!isApiError(error, 'unauthenticated')) {
      SingleToast.push(dbRefusal(error) ?? texts.common.actionFailed, { use: 'error' });
    }
  };

  const confirmRemove = async () => {
    if (!removing) {
      return;
    }
    setRemovePending(true);
    try {
      await api.adminDeleteDbConnection(removing.id);
      SingleToast.push(a.removed);
    } catch (error) {
      refused(error);
    } finally {
      setRemovePending(false);
      setRemoving(null);
      reload();
    }
  };

  const sorted = items && [...items].sort((x, y) => x.title.localeCompare(y.title, 'ru') * (order === 'asc' ? 1 : -1));
  const addButton = (
    <span data-opener="db-add">
      <Button use="primary" onClick={() => openForm({ connection: null, focusCheck: false }, '[data-opener="db-add"] button')}>
        {a.add}
      </Button>
    </span>
  );

  let body: React.ReactNode;
  if (failed && !items) {
    body = (
      <Notice kind="error" action={{ label: texts.common.retry, onClick: reload }}>
        {a.loadFailed}
      </Notice>
    );
  } else if (!sorted) {
    body = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.placeholder} />
      </Loader>
    );
  } else if (sorted.length === 0) {
    body = (
      <EmptyState
        title={a.empty.title}
        text={a.empty.text}
        action={{ label: a.add, onClick: () => openForm({ connection: null, focusCheck: false }, '[data-opener="db-add"] button') }}
      />
    );
  } else {
    body = (
      <DataTable>
        <thead>
          <tr>
            <SortableHeader order={order} onToggle={() => setOrder((value) => (value === 'asc' ? 'desc' : 'asc'))}>
              {a.columns.title}
            </SortableHeader>
            <th scope="col">{a.columns.base}</th>
            <th scope="col">{a.columns.access}</th>
            <th scope="col">{a.columns.samples}</th>
            <th scope="col">{a.columns.rights}</th>
            <th scope="col">{a.columns.snapshot}</th>
            <th scope="col">
              <span className="p-visually-hidden">{texts.users.actions}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((connection) => {
            const opener = `[data-opener="db-${connection.id}"] [tabindex="0"]`;
            const toCheck = () => openForm({ connection, focusCheck: true }, opener);
            return (
              <tr key={connection.id}>
                <td className={styles.title}>
                  <span>{connection.title}</span>
                  {connection.description && (
                    <TitleHint text={connection.description}>
                      <span className={styles.description}>{connection.description}</span>
                    </TitleHint>
                  )}
                </td>
                <td>
                  <span className="p-mono">
                    {connection.host}:{connection.port}/{connection.database}
                  </span>
                  {connection.tls_mode === 'disable' && (
                    <span className={styles.secondLine}>
                      <span className={styles.warningIcon} aria-hidden="true">
                        <IconWarningTriangleRegular16 />
                      </span>
                      {a.noTls}
                    </span>
                  )}
                  {connection.tls_mode === 'require' && <span className={styles.muted}>{a.noVerify}</span>}
                </td>
                <td>
                  {connection.access.all ? (
                    a.accessAll
                  ) : (
                    <Hint text={(connection.access.users ?? []).map((user) => user.full_name).join(', ')}>
                      <span tabIndex={0}>{a.accessUsers(connection.access.users?.length ?? 0)}</span>
                    </Hint>
                  )}
                </td>
                <td>
                  {connection.samples_allowed ? a.samplesOn : a.samplesOff}
                  {connection.samples_allowed && connection.sample_exclusions.length > 0 && (
                    <span className={styles.muted}>{a.exclusions(connection.sample_exclusions.length)}</span>
                  )}
                </td>
                <td>
                  <RightsCell connection={connection} checking={checking.has(connection.id)} onOpen={toCheck} />
                </td>
                <td>
                  {connection.snapshot ? (
                    <>
                      {a.snapshotTables(connection.snapshot.tables)}
                      <span className={styles.muted}>{snapshotDay(connection.snapshot.taken_at)}</span>
                    </>
                  ) : (
                    a.noSnapshot
                  )}
                </td>
                <td className={styles.kebab} data-opener={`db-${connection.id}`}>
                  <Hint text={texts.users.actions}>
                    <Kebab aria-label={`${texts.users.actions}: ${connection.title}`}>
                      <MenuItem onClick={() => openForm({ connection, focusCheck: false }, opener)}>{a.menu.edit}</MenuItem>
                      <MenuItem onClick={() => void check(connection)}>{a.menu.check}</MenuItem>
                      <MenuItem onClick={() => void refresh(connection)}>{a.menu.refresh}</MenuItem>
                      <MenuSeparator />
                      <MenuItem
                        onClick={() => {
                          rememberOpener(opener);
                          setRemoving(connection);
                        }}
                      >
                        {a.menu.remove}
                      </MenuItem>
                    </Kebab>
                  </Hint>
                </td>
              </tr>
            );
          })}
        </tbody>
      </DataTable>
    );
  }

  return (
    <>
      <AdminHeader title={a.title} action={addButton} />
      <ScrollContainer>
        <div className={styles.content}>
          <p className={styles.privacy}>{a.privacy}</p>
          {failed && items && (
            <Notice kind="error" action={{ label: texts.common.retry, onClick: reload }}>
              {a.loadFailed}
            </Notice>
          )}
          {body}
        </div>
      </ScrollContainer>
      {form && config && (
        <ConnectionForm
          connection={form.connection}
          config={config}
          focusCheck={form.focusCheck}
          onChanged={reload}
          onFeatureOff={featureOff}
          onClose={() => setForm(null)}
        />
      )}
      {removing && (
        <ConfirmModal
          title={a.removeTitle(removing.title)}
          body={a.removeBody}
          action={a.menu.remove}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </>
  );
}

/** «Проверка прав» в списке (§5.14): итог и время; лишние права и сбой — ссылки на блок «Проверка». */
function RightsCell({ connection, checking, onOpen }: { connection: AdminDbConnection; checking: boolean; onOpen: () => void }) {
  if (checking) {
    return <Spinner type="mini" caption={a.checking} />;
  }
  const { rights, last_error: lastError } = connection;
  const time = rights.checked_at ? <span className={styles.muted}>{formatDate(rights.checked_at)}</span> : null;
  const failedLine = lastError && rights.status !== 'unknown' && (
    <span className={styles.secondLine}>
      <Link component="button" onClick={onOpen}>
        {a.lastFailed}
      </Link>
    </span>
  );
  if (rights.status === 'read_only') {
    return (
      <>
        <span className={styles.status}>
          <span className={styles.icon} aria-hidden="true">
            <IconCheckARegular16 />
          </span>
          {a.readOnly}
        </span>
        {time}
        {rights.warnings.length > 0 && (
          <span className={styles.secondLine}>
            <span className={styles.warningIcon} aria-hidden="true">
              <IconWarningTriangleRegular16 />
            </span>
            <Link component="button" onClick={onOpen}>
              {a.hasWarning}
            </Link>
          </span>
        )}
        {failedLine}
      </>
    );
  }
  if (rights.status === 'excess') {
    return (
      <>
        <span className={styles.status}>
          <span className={styles.errorIcon} aria-hidden="true">
            <IconXCircleRegular16 />
          </span>
          <Link component="button" onClick={onOpen}>
            {a.excess(excessCount(connection))}
          </Link>
        </span>
        {time}
        {failedLine}
      </>
    );
  }
  if (lastError) {
    return (
      <>
        <span className={styles.status}>
          <span className={styles.errorIcon} aria-hidden="true">
            <IconXCircleRegular16 />
          </span>
          <Link component="button" onClick={onOpen}>
            {a.connectFailed}
          </Link>
        </span>
        <span className={styles.muted}>{formatDate(lastError.at)}</span>
      </>
    );
  }
  return <span className={styles.muted}>{a.notChecked}</span>;
}
