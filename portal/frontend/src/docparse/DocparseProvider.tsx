import { SingleToast } from '@skbkontur/react-ui';
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';

import { api, isAbort, isApiError, NetworkError } from '../api/client';
import type { DocparseResult, PortalConfig } from '../api/types';
import { useChat } from '../chat/ChatProvider';
import { megabytes } from '../kb/documents';
import { errorText, texts } from '../texts';

const t = texts.docparse;
const BASE_PATH = '/documents';

/** Форма нового разбора; файл остаётся в памяти вкладки, пока разбор не дал таблицу. */
export interface DocparseForm {
  file: File | null;
  templateId: string | null;
  fileError: string | null;
  templateError: string | null;
  /** Заметка над формой после сбоя до таблицы; `retry` — предложить «Разобрать заново». */
  notice: { text: string; retry: boolean } | null;
  stopped: boolean;
}

/** Идущий разбор — один на приложение. */
export interface DocparseRun {
  fileName: string;
  templateTitle: string;
  phase: 'uploading' | 'reading' | 'extracting' | 'summary';
  pages: { from: number; to: number; total: number } | null;
  /** Появляется с событием `extraction`: с этого момента разбор сохранён. */
  dialogId: string | null;
}

/** Результат разбора на экране; `summaryError` — причина обрыва, известная только из потока. */
export interface DocparseView {
  result: DocparseResult;
  summaryError?: 'overloaded';
}

interface DocparseApi {
  form: DocparseForm;
  run: DocparseRun | null;
  results: Record<string, DocparseView | 'not_found' | 'failed'>;
  setFile: (file: File | null) => void;
  setTemplate: (templateId: string) => void;
  start: () => void;
  /** До таблицы — отменяет разбор и возвращает форму; после — останавливает краткое содержание. */
  stop: () => void;
  load: (id: string) => void;
  forget: (id: string) => void;
}

const EMPTY_FORM: DocparseForm = {
  file: null,
  templateId: null,
  fileError: null,
  templateError: null,
  notice: null,
  stopped: false,
};

const DocparseContext = createContext<DocparseApi | null>(null);

export function useDocparse(): DocparseApi {
  const value = useContext(DocparseContext);
  if (!value) {
    throw new Error('useDocparse вызван вне DocparseProvider');
  }
  return value;
}

/** Проверка файла до отправки: расширение и размер (TXT и MD здесь не принимаются). */
function checkFile(file: File, limits: PortalConfig['docparse']): string | null {
  const name = file.name.toLowerCase();
  if (!limits.document_extensions.some((extension) => name.endsWith(extension))) {
    return texts.kb.upload.unsupported;
  }
  return file.size > limits.document_max_bytes ? texts.kb.upload.tooLarge(megabytes(limits.document_max_bytes)) : null;
}

/** Отказ до начала разбора — под областью выбора файла; `null` — отказ не про файл. */
function fileRefusal(error: unknown, limits: PortalConfig['docparse']): string | null {
  if (isApiError(error, 'too_many_pages')) {
    const max = error.details.max_pages;
    return t.tooManyPages(typeof max === 'number' ? max : limits.max_pages);
  }
  if (isApiError(error, 'document_too_long')) {
    return t.tooLong;
  }
  if (isApiError(error, 'file_too_large')) {
    const max = error.details.max_bytes;
    return texts.kb.upload.tooLarge(megabytes(typeof max === 'number' ? max : limits.document_max_bytes));
  }
  if (isApiError(error, 'unsupported_file_type')) {
    return texts.kb.upload.unsupported;
  }
  return isApiError(error, 'file_unreadable') ? texts.kb.upload.unreadable : null;
}

/** Заметка над формой по событию `error`, пришедшему до таблицы (концепция §5.9). */
function runFailure(
  code: string,
  message: string,
  details?: { page: number; pages_total: number },
): { text: string; retry: boolean } {
  switch (code) {
    case 'recognition_failed':
      // Страницы прочитаны, текста нет: повтор с тем же файлом даст тот же итог.
      return { text: t.errors.recognition, retry: false };
    case 'document_too_long':
      return { text: t.tooLong, retry: false };
    case 'generation_timeout':
      return { text: t.errors.timeout, retry: true };
    case 'model_overloaded':
      return { text: t.errors.overloaded, retry: true };
    case 'model_unavailable':
    case 'internal_error':
      return { text: details ? t.errors.brokenAt(details.page, details.pages_total) : t.errors.broken, retry: true };
    default:
      // Кода нет в словаре: текст сервера.
      return { text: message || t.errors.broken, retry: true };
  }
}

/**
 * Разбор документов (концепция §5.9). Запрос-поток держит приложение, а не экран: переход в другой
 * раздел разбор не прерывает. С события `extraction` разбор сохранён на сервере и есть в истории.
 */
export function DocparseProvider({ config, children }: { config: PortalConfig | null; children: React.ReactNode }) {
  const { addDialog, forgetDialog } = useChat();
  const navigate = useNavigate();
  const location = useLocation();
  const [form, setForm] = useState(EMPTY_FORM);
  const [run, setRun] = useState<DocparseRun | null>(null);
  const [results, setResults] = useState<DocparseApi['results']>({});
  const controller = useRef<AbortController | null>(null);
  /** Разбор, поток которого идёт в этой вкладке (с события `extraction`). */
  const liveId = useRef<string | null>(null);
  const pathname = useRef(location.pathname);
  const formRef = useRef(form);

  useEffect(() => {
    pathname.current = location.pathname;
    formRef.current = form;
  }, [location.pathname, form]);

  useEffect(() => () => controller.current?.abort(), []);

  // Пока таблицы нет, обновление или закрытие вкладки прервёт разбор — стандартное предупреждение браузера.
  const beforeTable = run !== null && run.dialogId === null;
  useEffect(() => {
    if (!beforeTable) {
      return;
    }
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [beforeTable]);

  const patchResult = useCallback((id: string, update: (view: DocparseView) => DocparseView) => {
    setResults((current) => {
      const view = current[id];
      return view && typeof view === 'object' ? { ...current, [id]: update(view) } : current;
    });
  }, []);

  const setFile = useCallback(
    (file: File | null) =>
      setForm((current) => ({
        ...current,
        file,
        fileError: file && config ? checkFile(file, config.docparse) : null,
        notice: null,
        stopped: false,
      })),
    [config],
  );

  const setTemplate = useCallback(
    (templateId: string) => setForm((current) => ({ ...current, templateId, templateError: null })),
    [],
  );

  const load = useCallback((id: string) => {
    api.getDialog(id).then(
      (dialog) => {
        const result = dialog.docparse;
        // Поток этого разбора идёт здесь и точнее сервера: его содержимое ответ сервера не затирает.
        setResults((current) => {
          const known = current[id];
          if (liveId.current === id) {
            return current;
          }
          // Причина обрыва известна только из потока: ответ сервера её не стирает.
          const summaryError =
            known && typeof known === 'object' && result?.summary_status === 'error' ? known.summaryError : undefined;
          return { ...current, [id]: result ? { result, summaryError } : 'not_found' };
        });
      },
      (error: unknown) => {
        if (isApiError(error, 'unauthenticated')) {
          return;
        }
        const notFound = isApiError(error, 'not_found');
        // Сбой перечитывания не стирает уже показанный разбор.
        setResults((current) =>
          !notFound && typeof current[id] === 'object' ? current : { ...current, [id]: notFound ? 'not_found' : 'failed' },
        );
      },
    );
  }, []);

  const start = useCallback(() => {
    const { file, templateId } = formRef.current;
    if (controller.current) {
      return;
    }
    // Без настроек (они не загрузились) файл проверяет только сервер, а шаблонов нет: на их месте — заметка.
    const limits = config?.docparse ?? null;
    const fileError = file ? limits && checkFile(file, limits) : t.chooseFile;
    const templateError = templateId || !limits ? null : t.chooseTemplate;
    setForm((current) => ({ ...current, fileError, templateError, notice: null, stopped: false }));
    const template = limits?.templates.find((item) => item.id === templateId);
    if (!file || !limits || !template || fileError) {
      return;
    }

    const abort = new AbortController();
    controller.current = abort;
    setRun({ fileName: file.name, templateTitle: template.title, phase: 'uploading', pages: null, dialogId: null });
    let dialogId: string | null = null;
    // Сервер знает больше потока (число страниц, очищенное имя файла, исход после обрыва связи):
    // по окончании потока разбор перечитывается. После остановки и удаления перечитывать нечего.
    let reread = false;

    const failBeforeTable = (notice: DocparseForm['notice'], fileRefused: string | null = null) => {
      setRun(null);
      setForm((current) => ({ ...current, notice, fileError: fileRefused ?? current.fileError }));
    };

    void (async () => {
      try {
        for await (const event of api.startDocparse(file, template.id, abort.signal)) {
          if (event.type === 'progress') {
            const pages = { from: event.page_from, to: event.page_to, total: event.pages_total };
            setRun((current) => current && { ...current, phase: 'reading', pages });
          } else if (event.type === 'extraction_started') {
            setRun((current) => current && { ...current, phase: 'extracting' });
          } else if (event.type === 'extraction') {
            const id = event.dialog_id;
            dialogId = id;
            liveId.current = id;
            const now = new Date().toISOString();
            const result: DocparseResult = {
              file_name: file.name,
              page_count: null,
              template_id: template.id,
              template_title: template.title,
              free_form: template.free_form,
              fields: event.fields,
              summary: '',
              summary_status: 'streaming',
            };
            setResults((current) => ({ ...current, [id]: { result } }));
            addDialog({ id, kind: 'docparse', title: file.name, created_at: now, updated_at: now });
            setRun((current) => current && { ...current, phase: 'summary', dialogId: id });
            setForm(EMPTY_FORM);
            if (pathname.current === BASE_PATH) {
              navigate(`${BASE_PATH}/${id}`, { replace: true });
            } else if (!pathname.current.startsWith(`${BASE_PATH}/`)) {
              // Сотрудник в другом разделе: сообщаем, что таблица готова.
              SingleToast.push(t.tableReady, {
                action: { label: t.open, handler: () => navigate(`${BASE_PATH}/${id}`) },
              });
            }
          } else if (event.type === 'delta' && dialogId) {
            patchResult(dialogId, (view) => ({ result: { ...view.result, summary: view.result.summary + event.text } }));
          } else if (event.type === 'done' && dialogId) {
            patchResult(dialogId, (view) => ({ result: { ...view.result, summary_status: event.status } }));
            reread = true;
          } else if (event.type === 'error') {
            if (event.code === 'dialog_deleted' && dialogId) {
              // Разбор удалён в другой вкладке: экран закрывается, строка убирается из истории.
              forgetDialog(dialogId);
              SingleToast.push(texts.dialogs.docparse.removedElsewhere);
              if (pathname.current === `${BASE_PATH}/${dialogId}`) {
                navigate(BASE_PATH, { replace: true });
              }
            } else if (dialogId) {
              const overloaded = event.code === 'model_overloaded';
              patchResult(dialogId, (view) => ({
                result: { ...view.result, summary_status: 'error' },
                summaryError: overloaded ? 'overloaded' : undefined,
              }));
              reread = true;
            } else if (event.code !== 'session_ended') {
              failBeforeTable(runFailure(event.code, event.message, event.details));
            }
          }
        }
      } catch (error) {
        const aborted = isAbort(error);
        if (dialogId) {
          // Таблица сохранена: остановилось только краткое содержание.
          patchResult(dialogId, (view) => ({ result: { ...view.result, summary_status: 'stopped' } }));
          if (!aborted) {
            SingleToast.push(t.connectionLost, { use: 'error' });
            reread = true;
          }
        } else if (aborted) {
          setRun(null);
          setForm((current) => ({ ...current, stopped: true }));
        } else if (error instanceof NetworkError) {
          failBeforeTable({ text: t.errors.network, retry: true });
        } else if (!isApiError(error, 'unauthenticated')) {
          const refused = fileRefusal(error, limits);
          failBeforeTable(refused ? null : { text: errorText(error), retry: true }, refused);
        }
      } finally {
        controller.current = null;
        liveId.current = null;
        setRun(null);
        if (dialogId && reread) {
          load(dialogId);
        }
      }
    })();
  }, [addDialog, config, forgetDialog, load, navigate, patchResult]);

  const stop = useCallback(() => controller.current?.abort(), []);

  const forget = useCallback((id: string) => {
    setResults((current) => Object.fromEntries(Object.entries(current).filter(([key]) => key !== id)));
  }, []);

  const value = useMemo<DocparseApi>(
    () => ({ form, run, results, setFile, setTemplate, start, stop, load, forget }),
    [form, run, results, setFile, setTemplate, start, stop, load, forget],
  );

  return <DocparseContext.Provider value={value}>{children}</DocparseContext.Provider>;
}
