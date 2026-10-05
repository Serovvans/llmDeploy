import { IconCopyRegular16 } from '@skbkontur/icons/IconCopyRegular16';
import { IconMediaUiAStopRegular16 } from '@skbkontur/icons/IconMediaUiAStopRegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Button, FileUploader, Gapped, Hint, Kebab, Loader, MenuItem, Radio, RadioGroup, SingleToast, Spinner } from '@skbkontur/react-ui';
import { useEffect, useId, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';

import type { Dialog, DocparseField, PortalConfig } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { exportDocx } from '../chat/exportDocx';
import { EMPTY_DRAFT } from '../chat/state';
import { Composer } from '../components/chat/Composer';
import { HistoryPanel } from '../components/chat/HistoryPanel';
import { MarkdownView } from '../components/chat/MarkdownView';
import { MessageList } from '../components/chat/MessageList';
import { ConfigNotice } from '../components/ConfigNotice';
import { ConfirmModal } from '../components/ConfirmModal';
import { DataTable } from '../components/DataTable';
import { EmptyState } from '../components/EmptyState';
import { Notice } from '../components/Notice';
import { PageHeader } from '../components/PageHeader';
import { StepList } from '../components/StepList';
import { useDocparse, type DocparseRun, type DocparseView } from '../docparse/DocparseProvider';
import { useCopy } from '../hooks/useCopy';
import { useFocusReturn } from '../hooks/useFocusReturn';
import { usePageTitle } from '../hooks/usePageTitle';
import { megabytes } from '../kb/documents';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import styles from './DocumentsScreen.module.css';

const t = texts.docparse;
const labels = texts.dialogs.docparse;
const BASE_PATH = '/documents';
const POLL_MS = 5000;
/** Номера и даты — моноширинным шрифтом: их переносят посимвольно. */
const NUMERIC = /^[\d\s.,:;/\\\-–—№()+]+$/;

/** `limits` равно `null`, когда настройки не загрузились: на месте шаблонов — заметка с «Повторить». */
function DocparseFormView({ limits }: { limits: PortalConfig['docparse'] | null }) {
  const { form, setFile, setTemplate, start } = useDocparse();
  const { reloadConfig } = useSession();
  const uploader = useRef<FileUploader>(null);
  const fileErrorId = useId();

  return (
    <div className={styles.form}>
      {form.notice && (
        <Notice kind="error" action={form.notice.retry ? { label: t.retry, onClick: start } : undefined}>
          {form.notice.text}
        </Notice>
      )}
      <StepList
        steps={[
          {
            title: t.stepFile,
            children: (
              <>
                <FileUploader
                  ref={uploader}
                  hideFiles
                  width="100%"
                  accept={limits?.document_extensions.join(',')}
                  uploaderText={t.pick}
                  error={form.fileError !== null}
                  aria-describedby={form.fileError ? fileErrorId : undefined}
                  onAttach={(attached) => {
                    setFile(attached[0]?.originalFile ?? null);
                    uploader.current?.reset();
                  }}
                />
                {form.file && (
                  <p className={styles.file}>
                    <span className={styles.fileName}>{form.file.name}</span>
                    <button
                      type="button"
                      className={styles.remove}
                      aria-label={t.removeFile(form.file.name)}
                      onClick={() => setFile(null)}
                    >
                      <IconXRegular16 aria-hidden="true" />
                    </button>
                  </p>
                )}
                {form.fileError && (
                  <p id={fileErrorId} className={styles.error} role="alert">
                    {form.fileError}
                  </p>
                )}
                {limits && <p className={styles.hint}>{t.limits(megabytes(limits.document_max_bytes), limits.max_pages)}</p>}
              </>
            ),
          },
          {
            title: t.stepTemplate,
            children: (
              <>
                <ConfigNotice />
                {limits && (
                  <div role="radiogroup" aria-label={t.stepTemplate}>
                    <RadioGroup<string> value={form.templateId ?? undefined} onValueChange={setTemplate}>
                      <Gapped vertical gap={8}>
                        {limits.templates.map((template) => (
                          <Radio<string> key={template.id} value={template.id}>
                            {template.title}
                            {template.description && <span className={styles.description}>{` — ${template.description}`}</span>}
                          </Radio>
                        ))}
                      </Gapped>
                    </RadioGroup>
                  </div>
                )}
                {form.templateError && (
                  <p className={styles.error} role="alert">
                    {form.templateError}
                  </p>
                )}
                <div className={styles.submit}>
                  <Button
                    use="primary"
                    onClick={() => {
                      // Шаблоны берутся из настроек: без них их загрузка повторяется сразу.
                      if (!limits) {
                        reloadConfig();
                      }
                      start();
                    }}
                  >
                    {t.submit}
                  </Button>
                  {form.stopped && <p className={styles.hint}>{t.stopped}</p>}
                </div>
              </>
            ),
          },
        ]}
      />
    </div>
  );
}

/** Ход разбора до таблицы: виден ход, а не безымянный индикатор (концепция §5.9). */
function DocparseProgress({ run, onStop }: { run: DocparseRun; onStop: () => void }) {
  let phase: string = t.reading;
  if (run.phase === 'uploading') {
    phase = t.uploading;
  } else if (run.phase === 'extracting') {
    phase = t.extracting;
  } else if (run.pages) {
    phase = t.readingPages(run.pages.from, run.pages.to, run.pages.total);
  }
  return (
    <div className={styles.progress}>
      <p className={styles.phase} role="status">
        <Spinner type="mini" caption={null} />
        <span>{phase}</span>
      </p>
      <p className={styles.hint}>{t.keepTab}</p>
      <div>
        <Button icon={<IconMediaUiAStopRegular16 />} onClick={onStop}>
          {t.stop}
        </Button>
      </div>
    </div>
  );
}

function FieldRow({ field }: { field: DocparseField }) {
  const copy = useCopy();
  const value = field.value;
  return (
    <tr className={styles.fieldRow}>
      <th scope="row" className={styles.fieldTitle}>
        {field.title}
      </th>
      <td>
        {value === null ? (
          <span className="p-muted">{t.absent}</span>
        ) : (
          <span className={NUMERIC.test(value) ? 'p-mono' : undefined}>{value}</span>
        )}
      </td>
      <td className={styles.copyCell}>
        {value !== null && (
          <Hint text={copy.label}>
            <button
              type="button"
              className={styles.copy}
              aria-label={t.copyValue(field.title)}
              onClick={() => void copy.copy(value)}
            >
              <IconCopyRegular16 aria-hidden="true" />
            </button>
          </Hint>
        )}
      </td>
    </tr>
  );
}

/** Таблица реквизитов и краткое содержание — над лентой вопросов по документу. */
function DocparseResultView({ view, live }: { view: DocparseView; live: boolean }) {
  const { result } = view;
  const status = result.summary_status;
  const elsewhere = status === 'streaming' && !live;
  return (
    <div className={styles.result}>
      <Notice kind="info">{t.verify}</Notice>
      <section className={styles.block}>
        <h2 className={styles.heading}>{result.free_form ? t.fieldsFree : t.fields}</h2>
        <DataTable>
          <thead>
            <tr>
              <th scope="col">{t.field}</th>
              <th scope="col">{t.value}</th>
              <th scope="col">
                <span className="p-visually-hidden">{texts.common.copy}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {result.fields.map((field, index) => (
              <FieldRow key={`${index}-${field.title}`} field={field} />
            ))}
          </tbody>
        </DataTable>
      </section>
      <section className={styles.block}>
        <h2 className={styles.heading}>{t.summary}</h2>
        {/* Содержание, которое пишется в другой вкладке, показано только индикатором. */}
        {result.summary && !elsewhere && <MarkdownView text={result.summary} streaming={status === 'streaming'} />}
        {status === 'streaming' && (elsewhere || !result.summary) && (
          <p className={styles.phase}>
            <Spinner type="mini" caption={null} />
            <span>{elsewhere ? t.summaryElsewhere : t.writingSummary}</span>
          </p>
        )}
        {status === 'length_limit' && <p className={styles.hint}>{t.summaryLengthLimit}</p>}
        {status === 'stopped' && <p className={styles.hint}>{t.summaryStopped}</p>}
        {status === 'error' && (
          <Notice kind="error">{view.summaryError === 'overloaded' ? t.summaryOverloaded : t.summaryError}</Notice>
        )}
      </section>
    </div>
  );
}

/** Разбор документов (концепция §5.9): форма, ход, результат с вопросами по документу. */
export function DocumentsScreen() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { config, configFailed } = useSession();
  const chat = useDialogs('docparse');
  const docparse = useDocparse();
  const { history, loadHistory, openDialog } = chat;
  const { run, load, forget } = docparse;
  const [removing, setRemoving] = useState<Dialog | null>(null);
  const [removePending, setRemovePending] = useState(false);
  const rememberOpener = useFocusReturn(removing !== null);

  const stored = id ? docparse.results[id] : undefined;
  const view = stored && typeof stored === 'object' ? stored : null;
  const live = run !== null && run.dialogId === id;
  const dialogView = id ? chat.state.dialogs[id] : undefined;
  const lastMessage = dialogView?.messages.at(-1);
  const answering = dialogView?.generation ?? null;
  const answerElsewhere = lastMessage?.status === 'streaming' && !answering;
  const summaryStreaming = view?.result.summary_status === 'streaming';
  const title = view ? `${view.result.file_name} · ${view.result.template_title}` : run && !id ? `${run.fileName} · ${run.templateTitle}` : t.title;
  usePageTitle(title);

  const historyIdle = history.status === 'idle';
  useEffect(() => {
    if (historyIdle) {
      loadHistory(false);
    }
  }, [historyIdle, loadHistory]);

  useEffect(() => {
    if (id) {
      load(id);
      openDialog(id);
    }
  }, [id, load, openDialog]);

  // Одновременно идёт один разбор: «Новый разбор» после таблицы открывает его результат.
  const runningId = run?.dialogId ?? null;
  useEffect(() => {
    if (!id && runningId) {
      navigate(`${BASE_PATH}/${runningId}`, { replace: true });
    }
  }, [id, runningId, navigate]);

  // Краткое содержание пишется в другой вкладке: разбор перезапрашивается, пока состояние не сменится.
  const summaryElsewhere = summaryStreaming && !live;
  useEffect(() => {
    if (!id || !summaryElsewhere) {
      return;
    }
    const timer = window.setInterval(() => load(id), POLL_MS);
    return () => window.clearInterval(timer);
  }, [id, summaryElsewhere, load]);

  // Разбор удалён (здесь или в другой вкладке): открывается форма нового разбора.
  const deleted = dialogView?.status === 'deleted';
  useEffect(() => {
    if (deleted) {
      navigate(BASE_PATH, { replace: true });
    }
  }, [deleted, navigate]);

  const startRemove = (target: Dialog, opener: string) => {
    rememberOpener(`[data-opener="${opener}"] [tabindex="0"]`);
    setRemoving(target);
  };

  const confirmRemove = async () => {
    if (!removing) {
      return;
    }
    setRemovePending(true);
    try {
      // Удаление во время краткого содержания: сначала закрывается поток разбора.
      if (run?.dialogId === removing.id) {
        docparse.stop();
      }
      await chat.remove(removing.id);
      forget(removing.id);
      SingleToast.push(labels.removed);
    } catch {
      SingleToast.push(labels.removeFailed, { use: 'error' });
    } finally {
      setRemovePending(false);
      setRemoving(null);
    }
  };

  const dialog: Dialog | null = id
    ? (history.items.find((item) => item.id === id) ?? {
        id,
        kind: 'docparse',
        title: view?.result.file_name ?? null,
        created_at: '',
        updated_at: '',
      })
    : null;

  let body: React.ReactNode;
  let composer: React.ReactNode = null;
  if (!id) {
    body =
      run && config ? (
        <DocparseProgress run={run} onStop={docparse.stop} />
      ) : config || configFailed ? (
        <DocparseFormView limits={config?.docparse ?? null} />
      ) : (
        <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
          <div className={styles.loading} />
        </Loader>
      );
  } else if (stored === 'not_found' || dialogView?.status === 'not_found') {
    body = (
      <EmptyState
        title={labels.notFound}
        text={texts.chat.notFound.text}
        action={{ label: labels.newLabel, onClick: () => navigate(BASE_PATH) }}
      />
    );
  } else if (stored === 'failed') {
    body = (
      <div className={styles.form}>
        <Notice kind="error" action={{ label: texts.common.retry, onClick: () => load(id) }}>
          {texts.common.actionFailed}
        </Notice>
      </div>
    );
  } else if (!view || !dialogView || dialogView.status !== 'ready') {
    body = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else {
    body = (
      <MessageList
        dialogId={id}
        view={dialogView}
        header={<DocparseResultView view={view} live={live} />}
        onRegenerate={() => chat.regenerate(id)}
        onLoadOlder={() => chat.loadOlder(id)}
        onOpenSource={() => undefined}
        openingFragment={null}
      />
    );
    // Пока содержание пишется, панель запроса ведёт себя как во время ответа: «Остановить» — для содержания.
    const busy = summaryStreaming ? (live ? 'generating' : 'waiting') : answering ? 'generating' : answerElsewhere ? 'waiting' : 'idle';
    composer = (
      <Composer
        dialogId={id}
        draft={chat.state.drafts[id] ?? EMPTY_DRAFT}
        answer={busy}
        placeholder={t.placeholder}
        waitText={summaryStreaming ? t.waitSummary : undefined}
        onTextChange={(text) => chat.setText(id, text)}
        onSend={() => chat.send(id, {}, () => undefined)}
        onStop={() => (summaryStreaming ? docparse.stop() : chat.stop(id))}
        onNotice={(notice) => chat.setNotice(id, notice)}
      />
    );
  }

  return (
    <div className={styles.screen}>
      <HistoryPanel
        history={history}
        basePath={BASE_PATH}
        newLabel={labels.newLabel}
        onNewChat={() => navigate(BASE_PATH)}
        onExport={(target) => void exportDocx(target.id)}
        onRemove={(target) => startRemove(target, `history-${target.id}`)}
        onShowMore={() => loadHistory(true)}
        onRetry={() => loadHistory(false)}
      />
      <div className={styles.area}>
        <PageHeader title={title}>
          {dialog && view && (
            <span data-opener="header">
              <Hint text={texts.users.actions} pos="left">
                <Kebab aria-label={`${texts.users.actions}: ${view.result.file_name}`}>
                  <MenuItem onClick={() => void exportDocx(dialog.id)}>{texts.chat.menu.exportDocx}</MenuItem>
                  <MenuItem onClick={() => startRemove(dialog, 'header')}>{texts.chat.menu.remove}</MenuItem>
                </Kebab>
              </Hint>
            </span>
          )}
        </PageHeader>
        <div className={styles.body}>{body}</div>
        {composer && lastMessage?.role === 'assistant' && lastMessage.dropped_messages > 0 && (
          <div className={styles.longDialog}>
            <Notice kind="info">{labels.longDialog}</Notice>
          </div>
        )}
        {composer}
      </div>
      {removing && (
        <ConfirmModal
          title={labels.removeTitle(removing.title ?? labels.newLabel)}
          body={labels.removeBody}
          action={texts.chat.menu.remove}
          pending={removePending}
          onConfirm={() => void confirmRemove()}
          onCancel={() => setRemoving(null)}
        />
      )}
    </div>
  );
}
