import { Button, Hint, Input, Kebab, Loader, MenuItem, Paging, ScrollContainer, SingleToast } from '@skbkontur/react-ui';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, USERS_PAGE_SIZE, type UserListQuery } from '../api/client';
import type { AdminUser, Page, TemporaryPasswordResult } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { DataTable, SortableHeader } from '../components/DataTable';
import { EmptyState } from '../components/EmptyState';
import { Notice } from '../components/Notice';
import { PageHeader } from '../components/PageHeader';
import { TitleHint } from '../components/TitleHint';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { useSession } from '../session/SessionContext';
import { errorText, texts } from '../texts';
import {
  CreateUserModal,
  EditUserModal,
  showUserActionError,
  TemporaryPasswordModal,
} from './UserDialogs';
import styles from './UsersScreen.module.css';

const SEARCH_DELAY_MS = 300;
const t = texts.users;

type ConfirmedAction = 'resetPassword' | 'resetSecondFactor' | 'block';

type Dialog =
  | { kind: 'create' }
  | { kind: 'edit'; user: AdminUser }
  | { kind: 'confirm'; action: ConfirmedAction; user: AdminUser }
  | { kind: 'temporaryPassword'; result: TemporaryPasswordResult };

type Loaded = { key: string; page: Page<AdminUser> } | { key: string; error: unknown };

export function UsersScreen() {
  usePageTitle(t.title);
  const { session } = useSession();
  const navigate = useNavigate();

  if (session?.user?.role !== 'admin') {
    return (
      <EmptyState
        headingLevel="h1"
        title={t.noAccess.title}
        text={t.noAccess.text}
        action={{ label: texts.common.goToChat, onClick: () => navigate('/chat') }}
      />
    );
  }
  return <UsersList />;
}

function UsersList() {
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState<UserListQuery>({ page: 1, q: '', order: 'asc' });
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [lastPage, setLastPage] = useState<Page<AdminUser> | null>(null);
  const [dialog, setDialog] = useState<Dialog | null>(null);
  const rememberOpener = useFocusReturn(dialog !== null);
  const [pending, setPending] = useState(false);

  const requestKey = JSON.stringify([query, reloads]);
  const loading = loaded?.key !== requestKey;

  useEffect(() => {
    let cancelled = false;
    api.listUsers(query).then(
      (page) => {
        if (!cancelled) {
          setLoaded({ key: requestKey, page });
          setLastPage(page);
        }
      },
      (error: unknown) => {
        if (!cancelled) {
          setLoaded({ key: requestKey, error });
        }
      },
    );
    return () => {
      cancelled = true;
    };
    // `requestKey` однозначно задаётся `query` и `reloads`.
  }, [query, requestKey]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const q = search.trim();
      setQuery((current) => (current.q === q ? current : { ...current, q, page: 1 }));
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [search]);

  const reload = () => setReloads((value) => value + 1);

  /** Открывает окно и запоминает, куда вернуть фокус: кнопка добавления или меню действий строки. */
  const openDialog = (next: Dialog, openerId: string) => {
    rememberOpener(`[data-opener="${openerId}"] :is(button, [tabindex="0"])`);
    setDialog(next);
  };

  const closeDialog = (changed: boolean) => {
    setDialog(null);
    if (changed) {
      reload();
    }
  };

  const unblock = async (user: AdminUser) => {
    try {
      await api.unblockUser(user.id);
      SingleToast.push(t.toast.unblocked);
    } catch (error) {
      showUserActionError(error);
    }
    reload();
  };

  const runConfirmed = async (action: ConfirmedAction, user: AdminUser) => {
    setPending(true);
    try {
      if (action === 'resetPassword') {
        setDialog({ kind: 'temporaryPassword', result: await api.resetUserPassword(user.id) });
      } else if (action === 'resetSecondFactor') {
        await api.resetUserSecondFactor(user.id);
        SingleToast.push(t.toast.secondFactorReset);
        setDialog(null);
      } else {
        await api.blockUser(user.id);
        SingleToast.push(t.toast.blocked);
        setDialog(null);
      }
    } catch (error) {
      showUserActionError(error);
      setDialog(null);
    } finally {
      setPending(false);
      reload();
    }
  };

  const page = lastPage;
  const failed = !loading && loaded && 'error' in loaded ? loaded.error : null;
  const pagesCount = page ? Math.ceil(page.total / USERS_PAGE_SIZE) : 0;

  return (
    <>
      <PageHeader title={t.title}>
        <span data-opener="add">
          <Button use="primary" onClick={() => openDialog({ kind: 'create' }, 'add')}>
            {t.add}
          </Button>
        </span>
      </PageHeader>
      <ScrollContainer>
        <div className={styles.content}>
          <Input
            width={320}
            placeholder={t.search}
            aria-label={t.search}
            value={search}
            onValueChange={setSearch}
          />
          <p className={styles.privacy}>{t.privacy}</p>
          {failed !== null && (
            <Notice kind="error" action={{ label: texts.common.retry, onClick: reload }}>
              {errorText(failed)}
            </Notice>
          )}
          <Loader active={loading} caption={texts.common.loading} delayBeforeSpinnerShow={300}>
            {page && page.items.length > 0 && (
              <DataTable>
                <thead>
                  <tr>
                    <SortableHeader
                      order={query.order}
                      onToggle={() =>
                        setQuery((current) => ({ ...current, order: current.order === 'asc' ? 'desc' : 'asc', page: 1 }))
                      }
                    >
                      {t.columns.fullName}
                    </SortableHeader>
                    <th scope="col">{t.columns.login}</th>
                    <th scope="col">{t.columns.role}</th>
                    <th scope="col">{t.columns.secondFactor}</th>
                    <th scope="col">{t.columns.state}</th>
                    <th scope="col">
                      <span className="p-visually-hidden">{t.actions}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((user) => (
                    <tr key={user.id}>
                      <td className={styles.name}>
                        <TitleHint text={user.full_name}>
                          <span className={styles.ellipsis}>{user.full_name}</span>
                        </TitleHint>
                        {user.is_me && <span className="p-muted"> {t.me}</span>}
                      </td>
                      <td className="p-mono">{user.login}</td>
                      <td>{t.role[user.role]}</td>
                      <td>{user.second_factor_configured ? t.secondFactorConfigured : t.secondFactorMissing}</td>
                      <td>{t.state[user.state]}</td>
                      <td className={styles.kebab} data-opener={user.id}>
                        {!user.is_me && (
                          <Hint text={t.actions}>
                            <Kebab aria-label={`${t.actions}: ${user.full_name}`}>
                              <MenuItem onClick={() => openDialog({ kind: 'edit', user }, user.id)}>{t.menu.edit}</MenuItem>
                              <MenuItem onClick={() => openDialog({ kind: 'confirm', action: 'resetPassword', user }, user.id)}>
                                {t.menu.resetPassword}
                              </MenuItem>
                              {user.second_factor_configured && (
                                <MenuItem
                                  onClick={() => openDialog({ kind: 'confirm', action: 'resetSecondFactor', user }, user.id)}
                                >
                                  {t.menu.resetSecondFactor}
                                </MenuItem>
                              )}
                              {user.state === 'blocked' ? (
                                <MenuItem onClick={() => void unblock(user)}>{t.menu.unblock}</MenuItem>
                              ) : (
                                <MenuItem onClick={() => openDialog({ kind: 'confirm', action: 'block', user }, user.id)}>
                                  {t.menu.block}
                                </MenuItem>
                              )}
                            </Kebab>
                          </Hint>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
            {page && page.items.length === 0 && (
              <EmptyState
                title={t.empty.title}
                text={t.empty.text}
                action={{ label: t.empty.action, onClick: () => setSearch('') }}
              />
            )}
            {!page && <div className={styles.placeholder} />}
          </Loader>
          {pagesCount > 1 && (
            <Paging
              activePage={query.page}
              pagesCount={pagesCount}
              onPageChange={(next) => setQuery((current) => ({ ...current, page: next }))}
            />
          )}
        </div>
      </ScrollContainer>

      {dialog?.kind === 'create' && <CreateUserModal onClose={closeDialog} />}
      {dialog?.kind === 'edit' && <EditUserModal user={dialog.user} onClose={closeDialog} />}
      {dialog?.kind === 'confirm' && (
        <ConfirmModal
          {...t.confirm[dialog.action]}
          who={t.confirm.who(dialog.user.full_name, dialog.user.login)}
          pending={pending}
          onConfirm={() => void runConfirmed(dialog.action, dialog.user)}
          onCancel={() => setDialog(null)}
        />
      )}
      {dialog?.kind === 'temporaryPassword' && (
        <TemporaryPasswordModal
          title={t.temporaryPassword.resetTitle}
          result={dialog.result}
          onClose={() => setDialog(null)}
        />
      )}
    </>
  );
}
