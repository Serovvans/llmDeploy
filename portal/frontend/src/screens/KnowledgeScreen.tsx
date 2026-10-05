import { IconSearchLoupeRegular16 } from '@skbkontur/icons/IconSearchLoupeRegular16';
import {
  Button,
  Hint,
  Input,
  Kebab,
  Loader,
  MenuItem,
  Paging,
  ScrollContainer,
  SingleToast,
  Spinner,
  Tabs,
} from '@skbkontur/react-ui';
import { useEffect, useState } from 'react';

import { api, isApiError, KB_PAGE_SIZE, type KbListQuery } from '../api/client';
import type { KbDocument, KbScope, Page } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { DataTable, SortableHeader } from '../components/DataTable';
import { EmptyState } from '../components/EmptyState';
import { AddDocumentsModal } from '../components/kb/AddDocumentsModal';
import { useDocumentViewer } from '../components/kb/DocumentViewer';
import { StatusBadge } from '../components/kb/StatusBadge';
import { Notice } from '../components/Notice';
import { PageHeader } from '../components/PageHeader';
import { TitleHint } from '../components/TitleHint';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { canRetry, isInProgress } from '../kb/documents';
import { useSession } from '../session/SessionContext';
import { errorText, formatDate, shortName, texts } from '../texts';
import styles from './KnowledgeScreen.module.css';

const t = texts.kb;
const SEARCH_DELAY_MS = 300;
const REFRESH_MS = 5000;
const DEFAULT_QUERY: KbListQuery = { scope: 'shared', page: 1, q: '', sort: 'created_at', order: 'desc' };

type Loaded = { key: string; page: Page<KbDocument> } | { key: string; error: unknown };

export function KnowledgeScreen() {
  usePageTitle(t.title);
  const { config } = useSession();
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [page, setPage] = useState<{ scope: KbScope; data: Page<KbDocument> } | null>(null);
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<KbDocument | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const rememberOpener = useFocusReturn(adding || removing !== null);
  const viewer = useDocumentViewer();

  const requestKey = JSON.stringify([query, reloads]);
  const loading = loaded?.key !== requestKey;

  useEffect(() => {
    let cancelled = false;
    api.listKbDocuments(query).then(
      (data) => {
        if (!cancelled) {
          setLoaded({ key: requestKey, page: data });
          setPage({ scope: query.scope, data });
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

  // Пока есть документы в очереди или в обработке, список обновляется сам — на месте, без индикатора.
  const documents = page?.scope === query.scope ? page.data.items : null;
  const inProgress = documents?.some(isInProgress) ?? false;
  useEffect(() => {
    if (!inProgress) {
      return;
    }
    let cancelled = false;
    const timer = window.setInterval(() => {
      api.listKbDocuments(query).then(
        (data) => {
          if (!cancelled) {
            setPage({ scope: query.scope, data });
          }
        },
        () => {
          // Фоновое обновление не удалось: следующая попытка — через 5 секунд.
        },
      );
    }, REFRESH_MS);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [inProgress, query]);

  const reload = () => setReloads((value) => value + 1);

  const sortBy = (sort: KbListQuery['sort']) =>
    setQuery((current) => ({
      ...current,
      page: 1,
      sort,
      // Повторное нажатие меняет порядок; новый столбец — со своего порядка по умолчанию.
      order: current.sort === sort ? (current.order === 'asc' ? 'desc' : 'asc') : sort === 'title' ? 'asc' : 'desc',
    }));

  const retry = async (document: KbDocument) => {
    try {
      await api.retryKbDocument(document.id);
      SingleToast.push(t.retried);
    } catch (error) {
      if (isApiError(error, 'forbidden')) {
        SingleToast.push(t.retryForbidden, { use: 'error' });
      } else if (!isApiError(error, 'document_not_in_error') && !isApiError(error, 'unauthenticated')) {
        SingleToast.push(errorText(error), { use: 'error' });
      }
    }
    reload();
  };

  const confirmRemove = async () => {
    if (!removing) {
      return;
    }
    setRemovePending(true);
    try {
      await api.deleteKbDocument(removing.id);
    } catch (error) {
      // Уже удалён в другой вкладке — то же самое, без сообщения об ошибке.
      if (!isApiError(error, 'not_found')) {
        SingleToast.push(t.remove.failed, { use: 'error' });
        setRemovePending(false);
        setRemoving(null);
        return;
      }
    }
    SingleToast.push(t.remove.done);
    // Строка убирается сразу.
    setPage((current) =>
      current && { ...current, data: { ...current.data, items: current.data.items.filter((item) => item.id !== removing.id) } },
    );
    setRemovePending(false);
    setRemoving(null);
    reload();
  };

  const openAdd = () => {
    rememberOpener('[data-opener="add"] button');
    setAdding(true);
  };

  const failed = !loading && loaded && 'error' in loaded ? loaded.error : null;
  const pagesCount = documents && page ? Math.ceil(page.data.total / KB_PAGE_SIZE) : 0;
  const shared = query.scope === 'shared';
  const empty = documents?.length === 0 && !loading && failed === null;

  return (
    <>
      <PageHeader title={t.title}>
        <span data-opener="add">
          <Button use="primary" disabled={!config} onClick={openAdd}>
            {t.add}
          </Button>
        </span>
      </PageHeader>
      <ScrollContainer>
        <div className={styles.content}>
          <div className={styles.toolbar}>
            <Tabs<KbScope>
              value={query.scope}
              onValueChange={(scope) => setQuery((current) => ({ ...current, scope, page: 1 }))}
            >
              <Tabs.Tab id="shared">{t.tabs.shared}</Tabs.Tab>
              <Tabs.Tab id="personal">{t.tabs.personal}</Tabs.Tab>
            </Tabs>
            <Input
              width={320}
              leftIcon={<IconSearchLoupeRegular16 />}
              placeholder={t.search}
              aria-label={t.search}
              value={search}
              onValueChange={setSearch}
            />
          </div>
          {failed !== null && (
            <Notice kind="error" action={{ label: texts.common.retry, onClick: reload }}>
              {errorText(failed)}
            </Notice>
          )}
          <Loader active={loading} caption={texts.common.loading} delayBeforeSpinnerShow={300}>
            {documents && documents.length > 0 && (
              <DataTable>
                <thead>
                  <tr>
                    <SortableHeader active={query.sort === 'title'} order={query.sort === 'title' ? query.order : 'asc'} onToggle={() => sortBy('title')}>
                      {t.columns.title}
                    </SortableHeader>
                    {shared && <th scope="col">{t.columns.author}</th>}
                    <SortableHeader
                      active={query.sort === 'created_at'}
                      order={query.sort === 'created_at' ? query.order : 'desc'}
                      onToggle={() => sortBy('created_at')}
                    >
                      {t.columns.added}
                    </SortableHeader>
                    <th scope="col">{t.columns.pages}</th>
                    <th scope="col">{t.columns.status}</th>
                    <th scope="col">
                      <span className="p-visually-hidden">{texts.users.actions}</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {documents.map((document) => (
                    <tr key={document.id}>
                      <td className={styles.title} data-opener={`title-${document.id}`}>
                        <TitleHint text={document.title}>
                          {document.status === 'ready' ? (
                            <button
                              type="button"
                              className={styles.open}
                              onClick={() =>
                                viewer.open(
                                  { documentId: document.id, title: document.title, document },
                                  `[data-opener="title-${document.id}"] button`,
                                )
                              }
                            >
                              {document.title}
                            </button>
                          ) : (
                            <span className={styles.name}>{document.title}</span>
                          )}
                        </TitleHint>
                        {viewer.opening?.documentId === document.id && (
                          <span className={styles.opening}>
                            <Spinner type="mini" caption={null} />
                          </span>
                        )}
                        {shared && document.is_cogis && <span className={styles.cogis}>{t.cogis}</span>}
                      </td>
                      {shared && (
                        <td className={styles.nowrap}>
                          {document.author.is_me ? t.me : shortName(document.author.full_name)}
                        </td>
                      )}
                      <td className={styles.nowrap}>{formatDate(document.created_at)}</td>
                      <td>{document.page_count ?? t.noPages}</td>
                      <td>
                        <StatusBadge document={document} />
                      </td>
                      <td className={styles.kebab} data-opener={`menu-${document.id}`}>
                        {document.can_delete && (
                          <Hint text={texts.users.actions} pos="left">
                            <Kebab aria-label={`${texts.users.actions}: ${document.title}`}>
                              {canRetry(document) && (
                                <MenuItem onClick={() => void retry(document)}>{t.menu.retry}</MenuItem>
                              )}
                              <MenuItem
                                onClick={() => {
                                  rememberOpener(`[data-opener="menu-${document.id}"] [tabindex="0"]`);
                                  setRemoving(document);
                                }}
                              >
                                {t.menu.remove}
                              </MenuItem>
                            </Kebab>
                          </Hint>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </DataTable>
            )}
            {empty && query.q && (
              <EmptyState
                title={t.empty.search.title}
                text={t.empty.search.text}
                action={{ label: t.empty.search.action, onClick: () => setSearch('') }}
              />
            )}
            {empty && !query.q && (
              <EmptyState
                title={t.empty[query.scope].title}
                text={t.empty[query.scope].text}
                action={{ label: t.add, onClick: openAdd }}
              />
            )}
            {!documents && <div className={styles.placeholder} />}
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

      {viewer.element}
      {adding && config && (
        <AddDocumentsModal limits={config.kb} scope={query.scope} onUploaded={reload} onClose={() => setAdding(false)} />
      )}
      {removing && (
        <ConfirmModal
          title={removing.scope === 'shared' ? t.remove.titleShared(removing.title) : t.remove.titlePersonal(removing.title)}
          body={removing.scope === 'shared' ? t.remove.bodyShared : t.remove.bodyPersonal}
          action={t.remove.action}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </>
  );
}
