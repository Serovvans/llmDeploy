import { IconSearchLoupeRegular16 } from '@skbkontur/icons/IconSearchLoupeRegular16';
import {
  Button,
  Hint,
  Input,
  Kebab,
  Link,
  Loader,
  MenuItem,
  Paging,
  ScrollContainer,
  SingleToast,
  Spinner,
  Tabs,
  TokenInput,
  TokenInputType,
} from '@skbkontur/react-ui';
import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { api, isApiError, KB_PAGE_SIZE, type KbListQuery } from '../api/client';
import type { KbDocument, KbScope, Page } from '../api/types';
import { ConfirmModal } from '../components/ConfirmModal';
import { DataTable, SortableHeader } from '../components/DataTable';
import { EmptyState } from '../components/EmptyState';
import { AddDocumentsModal } from '../components/kb/AddDocumentsModal';
import { useDocumentViewer } from '../components/kb/DocumentViewer';
import { EditTagsModal } from '../components/kb/EditTagsModal';
import { StatusBadge } from '../components/kb/StatusBadge';
import { TagChips } from '../components/kb/TagChips';
import { Notice } from '../components/Notice';
import { PageHeader } from '../components/PageHeader';
import { TitleHint } from '../components/TitleHint';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { canRetry, documentVolume, isInProgress, tagKey } from '../kb/documents';
import { useSession } from '../session/SessionContext';
import { errorText, formatDate, shortName, texts } from '../texts';
import styles from './KnowledgeScreen.module.css';

const t = texts.kb;
const SEARCH_DELAY_MS = 300;
const REFRESH_MS = 5000;
const DEFAULT_QUERY: KbListQuery = { scope: 'shared', page: 1, q: '', tags: [], sort: 'created_at', order: 'desc' };

const REMOVE_TITLES = {
  shared: t.remove.titleShared,
  personal: t.remove.titlePersonal,
  ministries: t.remove.titleMinistries,
} satisfies Record<KbScope, (title: string) => string>;

type Loaded = { key: string; page: Page<KbDocument> } | { key: string; error: unknown };

export function KnowledgeScreen() {
  usePageTitle(t.title);
  const { config, session } = useSession();
  const admin = session?.user?.role === 'admin';
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState(DEFAULT_QUERY);
  const [reloads, setReloads] = useState(0);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [page, setPage] = useState<{ scope: KbScope; data: Page<KbDocument> } | null>(null);
  // Переход из помощника CoGIS: окно добавления открыто сразу, выбраны общая база и отметка CoGIS.
  const fromCogis = (useLocation().state as { addCogisDocumentation?: boolean } | null)?.addCogisDocumentation === true;
  const [adding, setAdding] = useState(fromCogis);
  const [addingCogis, setAddingCogis] = useState(fromCogis);
  const navigate = useNavigate();
  // Признак перехода разовый: после обновления страницы окно не открывается снова.
  useEffect(() => {
    if (fromCogis) {
      navigate('.', { replace: true, state: null });
    }
  }, [fromCogis, navigate]);
  const [removing, setRemoving] = useState<KbDocument | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const [editingTags, setEditingTags] = useState<KbDocument | null>(null);
  const [tagHintsFailed, setTagHintsFailed] = useState(false);
  const rememberOpener = useFocusReturn(adding || removing !== null || editingTags !== null);
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

  // Пока есть документы в очереди, в обработке или с обновляемой обработкой, список обновляется сам — на месте,
  // без индикатора.
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

  /** Правит строки открытой страницы на месте: `change` возвращает новую строку или `null` — убрать её. */
  const updateRow = (id: string, change: (document: KbDocument) => KbDocument | null) =>
    setPage(
      (current) =>
        current && {
          ...current,
          data: {
            ...current.data,
            items: current.data.items.flatMap((item) => (item.id === id ? (change(item) ?? []) : item)),
          },
        },
    );

  const maxFilterTags = config?.kb.tags.max_selected ?? null;
  const filterFull = maxFilterTags !== null && query.tags.length >= maxFilterTags;

  /** Отбор по тегам складывается с поиском и возвращает список на первую страницу. */
  const filterByTags = (tags: string[]) => setQuery((current) => ({ ...current, tags, page: 1 }));

  /** Тег из строки добавляется в отбор; при достигнутом пределе и повторно — нет. */
  const pickTag = (tag: string) => {
    if (!filterFull && !query.tags.some((item) => tagKey(item) === tagKey(tag))) {
      filterByTags([...query.tags, tag]);
    }
  };

  const suggestTags = async (text: string): Promise<string[]> => {
    if (filterFull) {
      return [];
    }
    try {
      const { items } = await api.listKbTags([], text.trim());
      setTagHintsFailed(false);
      return items;
    } catch {
      // Подсказки не загрузились: «Такого тега нет» было бы неправдой, список просто не открывается.
      setTagHintsFailed(true);
      return [];
    }
  };

  const retry = async (document: KbDocument) => {
    try {
      // Строка перерисовывается по ответу сервера: что под «Готов», решают только поля документа.
      const updated = await api.retryKbDocument(document.id);
      updateRow(document.id, () => updated);
      SingleToast.push(document.status === 'ready' ? t.reprocessRetried : t.retried);
      return;
    } catch (error) {
      if (isApiError(error, 'source_unavailable')) {
        // Исходный файл записи уже удалён: до следующего обновления списка строка показывает эту причину.
        updateRow(document.id, (item) => ({ ...item, error_code: 'source_expired' }));
        return;
      }
      if (isApiError(error, 'forbidden')) {
        SingleToast.push(document.scope === 'ministries' ? t.ministriesForbidden : t.retryForbidden, { use: 'error' });
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
        const noRight = isApiError(error, 'forbidden') && removing.scope === 'ministries';
        SingleToast.push(noRight ? t.ministriesForbidden : t.remove.failed, { use: 'error' });
        setRemovePending(false);
        setRemoving(null);
        return;
      }
    }
    SingleToast.push(t.remove.done);
    const lastOnPage = page?.data.items.length === 1 && query.page > 1;
    // Строка убирается сразу.
    updateRow(removing.id, () => null);
    setRemovePending(false);
    setRemoving(null);
    if (lastOnPage) {
      // Удалена последняя строка страницы: переход на предыдущую, а не пустое состояние (концепция §6).
      setQuery((current) => ({ ...current, page: current.page - 1 }));
    } else {
      reload();
    }
  };

  const openAdd = () => {
    rememberOpener('[data-opener="add"] button');
    setAdding(true);
  };

  const openTags = (document: KbDocument, openerSelector: string) => {
    rememberOpener(openerSelector);
    setEditingTags(document);
  };

  const resetFilter = () => {
    setSearch('');
    setQuery((current) => ({ ...current, q: '', tags: [], page: 1 }));
  };

  const failed = !loading && loaded && 'error' in loaded ? loaded.error : null;
  const pagesCount = documents && page ? Math.ceil(page.data.total / KB_PAGE_SIZE) : 0;
  const { scope } = query;
  const withAuthor = scope !== 'personal';
  const empty = documents?.length === 0 && !loading && failed === null;

  return (
    <>
      <PageHeader title={t.title}>
        <span data-opener="add">
          <Button use="primary" onClick={openAdd}>
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
              <Tabs.Tab id="ministries">{t.tabs.ministries}</Tabs.Tab>
            </Tabs>
            <div className={styles.filters}>
              <div className={styles.tagFilter}>
                <TokenInput<string>
                  type={TokenInputType.WithReference}
                  width="100%"
                  placeholder={t.filter.placeholder}
                  aria-label={t.filter.placeholder}
                  aria-describedby={filterFull ? 'kb-filter-limit' : undefined}
                  selectedItems={query.tags}
                  getItems={suggestTags}
                  // Без строки «не найдено» список подсказок не открывается: при сбое и при достигнутом пределе его нет.
                  renderNotFound={() => (tagHintsFailed || filterFull ? null : t.filter.noTag)}
                  // Достигнут предел — новый тег не принимается; убрать тег можно всегда.
                  onValueChange={(next) => (next.length < query.tags.length || !filterFull) && filterByTags(next)}
                />
                {filterFull && (
                  <p id="kb-filter-limit" className={styles.note}>
                    {t.filter.limit(maxFilterTags)}
                  </p>
                )}
              </div>
              <Input
                width={320}
                leftIcon={<IconSearchLoupeRegular16 />}
                placeholder={t.search}
                aria-label={t.search}
                value={search}
                onValueChange={setSearch}
              />
            </div>
          </div>
          {scope === 'ministries' && <p className={styles.note}>{admin ? t.ministriesNote.admin : t.ministriesNote.employee}</p>}
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
                    <th scope="col">{t.columns.tags}</th>
                    {withAuthor && <th scope="col">{t.columns.author}</th>}
                    <SortableHeader
                      active={query.sort === 'created_at'}
                      order={query.sort === 'created_at' ? query.order : 'desc'}
                      onToggle={() => sortBy('created_at')}
                    >
                      {t.columns.added}
                    </SortableHeader>
                    <th scope="col">{t.columns.volume}</th>
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
                        {scope === 'shared' && document.is_cogis && <span className={styles.cogis}>{t.cogis}</span>}
                      </td>
                      <td data-opener={`tags-${document.id}`}>
                        {document.tags.length > 0 ? (
                          <TagChips tags={document.tags} onPick={pickTag} />
                        ) : (
                          document.can_delete && (
                            <Link component="button" onClick={() => openTags(document, `[data-opener="tags-${document.id}"] button`)}>
                              {t.tags.add}
                            </Link>
                          )
                        )}
                      </td>
                      {withAuthor && (
                        <td className={styles.nowrap}>
                          {document.author.is_me ? t.me : shortName(document.author.full_name)}
                        </td>
                      )}
                      <td className={styles.nowrap}>{formatDate(document.created_at)}</td>
                      <td className={styles.nowrap}>{documentVolume(document) ?? t.noVolume}</td>
                      <td>
                        <StatusBadge document={document} media={config?.media ?? null} />
                      </td>
                      <td className={styles.kebab} data-opener={`menu-${document.id}`}>
                        {document.can_delete && (
                          <Hint text={texts.users.actions} pos="left">
                            <Kebab aria-label={`${texts.users.actions}: ${document.title}`}>
                              <MenuItem onClick={() => openTags(document, `[data-opener="menu-${document.id}"] [tabindex="0"]`)}>
                                {t.menu.tags}
                              </MenuItem>
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
            {empty && query.tags.length > 0 && (
              <EmptyState
                title={t.empty.tags.title}
                text={t.empty.tags.text}
                action={{ label: t.empty.tags.action, onClick: resetFilter }}
              />
            )}
            {empty && query.tags.length === 0 && query.q && (
              <EmptyState
                title={t.empty.search.title}
                text={t.empty.search.text}
                action={{ label: t.empty.search.action, onClick: () => setSearch('') }}
              />
            )}
            {empty && query.tags.length === 0 && !query.q && scope === 'ministries' && (
              <EmptyState
                title={t.empty.ministries.title}
                text={admin ? t.empty.ministries.admin : t.empty.ministries.employee}
                action={admin ? { label: t.add, onClick: openAdd } : undefined}
              />
            )}
            {empty && query.tags.length === 0 && !query.q && scope !== 'ministries' && (
              <EmptyState title={t.empty[scope].title} text={t.empty[scope].text} action={{ label: t.add, onClick: openAdd }} />
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
      {adding && (
        <AddDocumentsModal
          config={config}
          scope={addingCogis ? 'shared' : scope}
          admin={admin}
          cogis={addingCogis}
          onUploaded={reload}
          onClose={() => {
            setAdding(false);
            setAddingCogis(false);
          }}
        />
      )}
      {editingTags && (
        <EditTagsModal
          document={editingTags}
          limits={config?.kb.tags ?? null}
          onSaved={(saved) => updateRow(saved.id, () => saved)}
          onForbidden={reload}
          onDeleted={() => updateRow(editingTags.id, () => null)}
          onClose={() => setEditingTags(null)}
        />
      )}
      {removing && (
        <ConfirmModal
          title={REMOVE_TITLES[removing.scope](removing.title)}
          body={removing.scope === 'personal' ? t.remove.bodyPersonal : t.remove.bodyShared}
          action={t.remove.action}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </>
  );
}
