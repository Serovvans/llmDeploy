import { IconArrowDim2DiagMaxRightRegular16 } from '@skbkontur/icons/IconArrowDim2DiagMaxRightRegular16';
import { IconArrowDim2DiagMinRightRegular16 } from '@skbkontur/icons/IconArrowDim2DiagMinRightRegular16';
import { IconArrowRoundSyncForwardRegular16 } from '@skbkontur/icons/IconArrowRoundSyncForwardRegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Hint, Link, Loader, MenuItem, Select, Spinner, Tabs } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../../api/client';
import type { CatalogClient, ClientLibraryFile, CodeFile, CodeProject, CodeWeb, DbConfig, PreviewLayerSample } from '../../api/types';
import { Notice } from '../../components/Notice';
import { dbRefusal } from '../../db/refusal';
import { texts } from '../../texts';
import { kindOf } from '../kinds';
import { readPreviewLayer, storePreviewLayer } from '../storage';
import styles from './PreviewPane.module.css';
import {
  appendMessage,
  CLIENT_WIDTH,
  EMPTY_LOG,
  errorCount,
  INVENTED_FEATURES,
  INVENTED_LAYER_NAME,
  type InitMessage,
  logResult,
  notInserted,
  parseFrameMessage,
  type PreviewLog,
  type PreviewProblem,
  shortUrl,
} from './preview';

const p = texts.workshop.web.pane;
const FRAME_URL = '/code-preview/frame.html';
/** Страница не прислала `ready` за это время — «Не удалось открыть предпросмотр». */
const READY_TIMEOUT_MS = 10000;
const ARGUMENT_SHORT = 40;

/** Тексты библиотек клиента CoGIS — один раз на вкладку (контракт §7.5.16). */
const libraryCache = new Map<ClientLibraryFile, Promise<string | null>>();

function library(file: ClientLibraryFile): Promise<string | null> {
  let cached = libraryCache.get(file);
  if (!cached) {
    cached = api.getCodeWebLibrary(file).then(
      (text) => text,
      (error: unknown) => {
        libraryCache.delete(file);
        if (isApiError(error, 'not_found')) {
          return null;
        }
        throw error;
      },
    );
    libraryCache.set(file, cached);
  }
  return cached;
}

/** Образец живёт, пока открыта вкладка; «Загрузить заново» берёт свежий (§5.13.21). */
const sampleCache = new Map<string, PreviewLayerSample>();

type Status = 'idle' | 'loading' | 'ready' | 'failed';

interface Props {
  project: CodeProject;
  web: CodeWeb | null;
  limits: { log_max_entries: number; errors_max: number };
  db: DbConfig;
  /** Меняется — предпросмотр запускается заново (кнопки строки «Путь до CoGIS»). */
  runKey: number;
  /** Несохранённая правка сначала сохраняется; `null` — сохранить не удалось. */
  prepare: () => Promise<CodeFile[] | null>;
  /** Код изменён после запуска (§5.13.21). */
  changed: boolean;
  expanded: boolean;
  onExpand: (expanded: boolean) => void;
  onRun: (files: CodeFile[]) => void;
  onProblems: (problems: PreviewProblem[]) => void;
  onJump: (path: string, line: number, column: number) => void;
  /** Предпросмотр или подключения выключили, пока колонка открыта. */
  onFeatureOff: () => void;
  /** Фокус на заголовок при открытии колонки (§9). */
  focusTitle: boolean;
}

function argumentsText(args: string[], full: boolean): string {
  return args.map((arg) => (full || arg.length <= ARGUMENT_SHORT ? arg : `${arg.slice(0, ARGUMENT_SHORT - 1)}…`)).join(', ');
}

function LogRow({ index, entry, result }: { index: number; entry: PreviewLog['entries'][number]; result: string }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={styles.logRow}>
      <button type="button" className={styles.logButton} aria-expanded={open} onClick={() => setOpen(!open)}>
        <span className={styles.number}>{index}</span>
        <span className={styles.call}>
          <span className={styles.mono}>{`api.${entry.fn}(${argumentsText(entry.args, false)})`}</span>
          <span className={styles.result}>
            {!entry.known && <IconWarningTriangleRegular16 className={styles.warningIcon} />}→ {result}
          </span>
        </span>
      </button>
      {open && <pre className={styles.args}>{argumentsText(entry.args, true)}</pre>}
    </li>
  );
}

function ProblemRow({ problem, paths, onJump }: { problem: PreviewProblem; paths: ReadonlySet<string>; onJump: Props['onJump'] }) {
  if (problem.kind !== 'error') {
    const text = problem.kind === 'image' ? p.image(shortUrl(problem.text)) : p.network(shortUrl(problem.text));
    return (
      <li className={styles.problem}>
        {problem.kind === 'image' ? <IconWarningTriangleRegular16 className={styles.warningIcon} /> : <IconXCircleRegular16 className={styles.errorIcon} />}
        <span>{text}</span>
      </li>
    );
  }
  const place = problem.file && problem.line ? texts.workshop.build.place(problem.file, problem.line) : (problem.file ?? p.placeUnknown);
  const file = problem.file;
  const line = problem.line;
  return (
    <li className={styles.problem}>
      <IconXCircleRegular16 className={styles.errorIcon} />
      <span className={styles.problemBody}>
        {file && line && paths.has(file) ? (
          <Link component="button" onClick={() => onJump(file, line, problem.column ?? 1)}>
            {place}
          </Link>
        ) : (
          <span>{place}</span>
        )}
        <code lang="en" className={styles.errorText}>
          {problem.text}
        </code>
      </span>
    </li>
  );
}

/** Колонка «Предпросмотр» (концепция §5.13.21): изолированный фрейм, журнал вызовов `api` и ошибки браузера. */
export function PreviewPane(props: Props) {
  const { project, web, limits, db, runKey, changed, expanded } = props;
  const kind = kindOf(project);
  const [status, setStatus] = useState<Status>('idle');
  const [log, setLog] = useState<PreviewLog>(EMPTY_LOG);
  const [frameKey, setFrameKey] = useState<number | null>(null);
  const [tab, setTab] = useState<'log' | 'errors'>('log');
  const [more, setMore] = useState(false);
  const [client, setClient] = useState<CatalogClient>('desktop');
  // Запомненная таблица слоя — если она по-прежнему отмечена в настройках проекта.
  const [layerTable, setLayerTable] = useState<string | null>(() => {
    const remembered = readPreviewLayer(project.id);
    const selected = db.enabled && project.db?.available ? project.db.tables.map((table) => `${table.schema}.${table.name}`) : [];
    return remembered && selected.includes(remembered) ? remembered : null;
  });
  const [sample, setSample] = useState<PreviewLayerSample | null>(null);
  const [sampleLoading, setSampleLoading] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);
  const [samplesAllowed, setSamplesAllowed] = useState<boolean | null>(null);
  const [ran, setRan] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const init = useRef<InitMessage | null>(null);
  const loads = useRef(0);
  const sent = useRef(false);
  const title = useRef<HTMLHeadingElement>(null);
  const latest = useRef(props);
  const clientRef = useRef(client);
  const sampleRef = useRef(sample);
  useEffect(() => {
    latest.current = props;
    clientRef.current = client;
    sampleRef.current = sample;
  });

  const selection = project.db;
  const withDb = db.enabled && selection !== null && selection !== undefined && selection.available;
  const tables = withDb ? selection.tables.map((table) => `${table.schema}.${table.name}`) : [];

  useEffect(() => {
    if (props.focusTitle) {
      title.current?.focus();
    }
  }, [props.focusTitle]);

  // Разрешены ли образцы базы — для пунктов списка «Данные слоя».
  const connectionId = withDb ? selection.connection_id : null;
  useEffect(() => {
    if (!connectionId) {
      return;
    }
    let cancelled = false;
    api.listDbConnections().then(
      ({ items }) => {
        if (!cancelled) {
          setSamplesAllowed(items.find((item) => item.id === connectionId)?.samples_allowed ?? false);
        }
      },
      () => {
        // Список не загрузился: пункты остаются доступными, отказ сервера покажет причину.
      },
    );
    return () => {
      cancelled = true;
    };
  }, [connectionId]);

  /** Образец таблицы для слоя; отказ — текст под списком и придуманный слой; `undefined` — флаг выключили. */
  const sampleFor = useCallback(async (projectId: string, table: string, fresh: boolean): Promise<PreviewLayerSample | null | undefined> => {
    const key = `${projectId}:${table}`;
    setRefusal(null);
    const cached = fresh ? undefined : sampleCache.get(key);
    if (cached) {
      setSample(cached);
      sampleRef.current = cached;
      return cached;
    }
    setSampleLoading(true);
    try {
      const loaded = await api.getPreviewLayer(projectId, { schema: table.slice(0, table.indexOf('.')), name: table.slice(table.indexOf('.') + 1) });
      sampleCache.set(key, loaded);
      setSample(loaded);
      sampleRef.current = loaded;
      return loaded;
    } catch (error) {
      if (isApiError(error, 'feature_disabled')) {
        latest.current.onFeatureOff();
        return undefined;
      }
      setRefusal(dbRefusal(error) ?? texts.common.actionFailed);
      setLayerTable(null);
      storePreviewLayer(projectId, null);
      setSample(null);
      sampleRef.current = null;
      return null;
    } finally {
      setSampleLoading(false);
    }
  }, []);

  /**
   * Запуск: сохранить файлы, при выборе таблицы — взять образец (из памяти вкладки или свежий), затем новый
   * фрейм. `load` — таблица слоя, которую нужно загрузить; без неё слой — текущий.
   */
  const run = useCallback(
    async (load?: { table: string; fresh: boolean }) => {
      const current = latest.current;
      setStatus('loading');
      setLog(EMPTY_LOG);
      current.onProblems([]);
      setFrameKey(null);
      const files = await current.prepare();
      if (!files) {
        setStatus('failed');
        return;
      }
      let layer = sampleRef.current;
      if (load) {
        const loaded = await sampleFor(current.project.id, load.table, load.fresh);
        if (loaded === undefined) {
          return;
        }
        layer = loaded;
      }
      let libraries: InitMessage['libraries'];
      try {
        const available = new Set((current.web?.libraries ?? []).filter((item) => item.available).map((item) => item.id));
        const [jquery, knockout, leaflet, leafletCss] = await Promise.all([
          available.has('jquery') ? library('jquery.js') : null,
          available.has('knockout') ? library('knockout.js') : null,
          available.has('leaflet') ? library('leaflet.js') : null,
          available.has('leaflet') ? library('leaflet.css') : null,
        ]);
        libraries = { jquery, knockout, leaflet, leaflet_css: leafletCss };
      } catch {
        setStatus('failed');
        return;
      }
      init.current = {
        type: 'init',
        kind: kindOf(current.project),
        files: [...files].sort((a, b) => a.path.localeCompare(b.path)).map((file) => ({ path: file.path, content: file.content, catalog: file.catalog ?? null })),
        client: kindOf(current.project) === 'catalog' ? clientRef.current : null,
        layer: layer
          ? { source: 'sample', name: `${layer.schema}.${layer.table}`, features: layer.features }
          : { source: 'invented', name: INVENTED_LAYER_NAME, features: INVENTED_FEATURES },
        libraries,
        api_functions: current.web?.api_functions ?? [],
        limits: current.limits,
      };
      loads.current = 0;
      sent.current = false;
      setRan(true);
      current.onRun(files);
      setFrameKey((key) => (key ?? 0) + 1);
    },
    [sampleFor],
  );

  // Сообщения только от окна текущего фрейма и только известных типов (§7.5.17).
  useEffect(() => {
    const listener = (event: MessageEvent) => {
      const target = frame.current?.contentWindow;
      if (!target || event.source !== target) {
        return;
      }
      const message = parseFrameMessage(event.data);
      if (!message) {
        return;
      }
      if (message.type === 'ready') {
        if (!sent.current && init.current) {
          sent.current = true;
          target.postMessage(init.current, '*');
          setStatus('ready');
        }
        return;
      }
      setLog((current) => appendMessage(current, message, latest.current.limits));
    };
    window.addEventListener('message', listener);
    return () => window.removeEventListener('message', listener);
  }, []);

  // Ошибки — в строку «Путь до CoGIS» и в вопрос «Исправить с помощником».
  const { onProblems } = props;
  useEffect(() => {
    onProblems(log.problems);
  }, [log.problems, onProblems]);

  useEffect(() => {
    if (status !== 'loading' || frameKey === null) {
      return;
    }
    const timer = window.setTimeout(() => {
      setFrameKey(null);
      setStatus('failed');
    }, READY_TIMEOUT_MS);
    return () => window.clearTimeout(timer);
  }, [frameKey, status]);

  // Запуск: при открытии колонки (с запомненным слоем из образца) и по каждому «Запустить заново».
  const firstRun = useRef(true);
  useEffect(() => {
    const first = firstRun.current;
    firstRun.current = false;
    void run(first && layerTable ? { table: layerTable, fresh: false } : undefined);
    // Runs on `runKey` only: the layer and the client start their own runs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runKey]);

  const chooseLayer = (value: string) => {
    const table = value === '' ? null : value;
    setLayerTable(table);
    storePreviewLayer(project.id, table);
    if (table) {
      void run({ table, fresh: false });
    } else {
      setSample(null);
      sampleRef.current = null;
      setRefusal(null);
      void run();
    }
  };

  const chooseClient = (value: CatalogClient) => {
    setClient(value);
    clientRef.current = value;
    void run();
  };

  const paths = new Set(project.files.map((file) => file.path));
  const errors = errorCount(log);
  const layerSource = { table: sample ? `${sample.schema}.${sample.table}` : null, rows: sample?.features.features.length ?? 0 };
  const noLibraries = web !== null && web.libraries.every((item) => !item.available);
  const firstFile = [...project.files].sort((a, b) => a.path.localeCompare(b.path))[0]?.path ?? '';
  const skipped = kind === 'catalog' ? notInserted(project.files, client) : [];
  const scale = kind === 'catalog' ? Math.min(1, 352 / CLIENT_WIDTH[client]) : 1;
  const unavailable = [...p.unavailable, ...(kind === 'catalog' ? [p.unavailableCatalog] : []), ...(kind === 'card_hooks' ? [p.unavailableHooks] : [])];

  let layerControl: React.ReactNode;
  if (!withDb) {
    layerControl = db.enabled ? (
      <Hint text={p.layerHint}>
        <span tabIndex={0}>{p.inventedText}</span>
      </Hint>
    ) : (
      <span>{p.inventedText}</span>
    );
  } else {
    const items = [
      ['', p.invented] as [string, string],
      ...tables.map((table) =>
        samplesAllowed === false ? (
          <MenuItem key={table} disabled comment={p.samplesNotAllowed}>
            {table}
          </MenuItem>
        ) : (
          ([table, table, p.sampleItem(db.sample_rows_preview)] as [string, string, string])
        ),
      ),
    ];
    layerControl = (
      <span className={styles.layerSelect}>
        <Hint text={p.layerPrivacy}>
          <span tabIndex={0}>{p.layer}</span>
        </Hint>
        <Select<string>
          size="small"
          width={200}
          value={layerTable ?? ''}
          items={items}
          aria-label={p.layer}
          onValueChange={chooseLayer}
          renderValue={(value) => (value === '' ? p.invented : value)}
        />
        {sampleLoading && <Spinner type="mini" caption={p.loadingSample} />}
      </span>
    );
  }

  let frameArea: React.ReactNode;
  if (status === 'failed') {
    frameArea = (
      <Notice kind="error" action={{ label: texts.common.retry, onClick: () => void run() }}>
        {p.failed}
      </Notice>
    );
  } else {
    const fileTitle = kind === 'catalog' ? project.name : firstFile;
    frameArea = (
      <Loader active={status === 'loading'} caption={p.loading} delayBeforeSpinnerShow={300} className={styles.loader}>
        <div className={styles.frameBox}>
          {frameKey !== null && (
            <iframe
              key={frameKey}
              ref={frame}
              className={styles.frame}
              title={p.frameTitle(fileTitle)}
              src={FRAME_URL}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              style={
                kind === 'catalog'
                  ? { width: CLIENT_WIDTH[client], height: `${100 / scale}%`, transform: `scale(${scale})`, transformOrigin: '0 0' }
                  : undefined
              }
              onLoad={() => {
                loads.current += 1;
                // The code changed the frame's address: the page is not ours any more (§7.5.17).
                if (loads.current > 1) {
                  setFrameKey(null);
                  setStatus('failed');
                }
              }}
            />
          )}
        </div>
      </Loader>
    );
  }

  let logContent: React.ReactNode;
  if (!ran || status === 'loading') {
    logContent = <p className={styles.note}>{p.logBefore}</p>;
  } else if (log.entries.length === 0) {
    logContent = <p className={styles.note}>{kind === 'js_plugin' ? p.logPlugin : p.logEmpty}</p>;
  } else {
    logContent = (
      <ol className={styles.log} aria-label={p.logLabel}>
        {log.entries.map((entry, index) => (
          <LogRow key={index} index={index + 1} entry={entry} result={logResult(entry, layerSource)} />
        ))}
        {log.overflow && <li className={styles.note}>{p.logLimit(limits.log_max_entries)}</li>}
      </ol>
    );
  }

  return (
    <aside className={expanded ? `${styles.pane} ${styles.expanded}` : styles.pane} aria-labelledby="preview-title" id="preview-pane">
      <div className={styles.head}>
        <h2 id="preview-title" ref={title} tabIndex={-1} className={styles.title}>
          {p.title}
        </h2>
        <Button size="small" icon={<IconArrowRoundSyncForwardRegular16 />} onClick={() => void run()}>
          {p.rerun}
        </Button>
        <Hint text={expanded ? p.collapse : p.expand} pos="left">
          <Button
            size="small"
            use="text"
            aria-label={expanded ? p.collapse : p.expand}
            aria-expanded={expanded}
            icon={expanded ? <IconArrowDim2DiagMinRightRegular16 /> : <IconArrowDim2DiagMaxRightRegular16 />}
            onClick={() => props.onExpand(!expanded)}
            onKeyDown={(event) => {
              if (event.key === 'Escape' && expanded) {
                props.onExpand(false);
              }
            }}
          />
        </Hint>
      </div>
      {changed && ran && (
        <p className={styles.changed}>
          <IconTimeClockRegular16 />
          {p.changed}
        </p>
      )}
      <div className={styles.settings}>
        {layerControl}
        {kind === 'catalog' && (
          <span className={styles.layerSelect}>
            <span>{p.client}</span>
            <Select<CatalogClient>
              size="small"
              value={client}
              items={(['desktop', 'tablet', 'smartphone'] as CatalogClient[]).map((item) => [item, p.clients[item]] as [CatalogClient, string])}
              aria-label={p.client}
              onValueChange={chooseClient}
            />
            <span className={styles.muted}>{p.scale(Math.round(scale * 100))}</span>
          </span>
        )}
      </div>
      {sample && !sampleLoading && (
        <p className={styles.origin}>
          {p.sampleOrigin(`${sample.schema}.${sample.table}`, sample.rows, sample.geometry !== null, sample.excluded_columns)}{' '}
          <Link component="button" onClick={() => layerTable && void run({ table: layerTable, fresh: true })}>
            {p.reloadSample}
          </Link>
        </p>
      )}
      {refusal && (
        <p className={styles.refusal} role="alert">
          {refusal}
        </p>
      )}
      <Notice kind="info">
        {sample ? p.notCogisSample : p.notCogis}{' '}
        <Link component="button" aria-expanded={more} onClick={() => setMore(!more)}>
          {p.more}
        </Link>
        {more && (
          <ul className={styles.unavailable}>
            {unavailable.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        )}
      </Notice>
      {noLibraries && <Notice kind="warning">{p.noLibraries}</Notice>}
      {skipped.length > 0 && <p className={styles.muted}>{p.notInserted(skipped.join(', '))}</p>}
      <div className={styles.frameArea}>{frameArea}</div>
      <div className={styles.journal}>
        <Tabs<'log' | 'errors'> value={tab} onValueChange={setTab} size="small">
          <Tabs.Tab id="log">{p.tabs.log(log.entries.length)}</Tabs.Tab>
          <Tabs.Tab id="errors">{p.tabs.errors(errors)}</Tabs.Tab>
        </Tabs>
        <div className={styles.journalBody}>
          {tab === 'log' ? (
            logContent
          ) : (
            <>
              <p className={styles.note}>{p.errorsNote}</p>
              {log.problems.length === 0 ? (
                <p className={styles.note}>{p.errorsEmpty}</p>
              ) : (
                <ul className={styles.problems}>
                  {log.problems.map((problem, index) => (
                    <ProblemRow key={index} problem={problem} paths={paths} onJump={props.onJump} />
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>
      <span className="p-visually-hidden" role="status">
        {status === 'ready' ? p.done(log.entries.length, errors) : ''}
      </span>
    </aside>
  );
}
