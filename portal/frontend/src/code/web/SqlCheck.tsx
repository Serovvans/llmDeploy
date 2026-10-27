import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { Button, Gapped, Input, Link, Modal, SingleToast, Textarea } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api, isApiError } from '../../api/client';
import type { CodeProject, PortalConfig, SqlRunBlock } from '../../api/types';
import { Line, SqlRunResult, sqlRunAnnouncement } from '../../components/chat/SqlRunResult';
import { Notice } from '../../components/Notice';
import { dbCheckUnavailable, dbRefusal } from '../../db/refusal';
import { useFocusReturn } from '../../hooks/useFocusReturn';
import { texts } from '../../texts';
import styles from './SqlCheck.module.css';
import { sqlParameters, stripQuotes } from './sqlParams';

const c = texts.workshop.web.sqlCheck;

/** Откуда запрос: файл и строки выделения (§5.13.23). */
export interface SqlSource {
  path: string;
  startLine: number;
  endLine: number;
}

interface Request {
  sql: string;
  values: Record<string, string>;
  source: SqlSource;
  /** Поле запроса правили — «запрос изменён вручную». */
  edited: boolean;
  /** Текст выделения без кавычек — по нему видно, правили ли поле. */
  original: string;
}

/** Редактор, из меню которого открыто окно (§9). */
const EDITOR = '[data-testid="code-editor"]';

const PENDING: SqlRunBlock = {
  index: 0,
  line: 1,
  status: 'pending',
  reason: null,
  fixture: null,
  result: null,
  affected_rows: null,
  error: null,
  fixed_sql: null,
};

interface ModalProps {
  request: Request;
  project: CodeProject;
  config: PortalConfig;
  /** Поля, у которых сервер не нашёл значения (`required`, `details.names`). */
  missing: string[];
  onSubmit: (request: Request) => void;
  onClose: () => void;
  onOpenSettings: () => void;
}

/** Окно «Проверить запрос» (концепция §5.13.23). */
function SqlCheckModal({ request, project, config, missing, onSubmit, onClose, onOpenSettings }: ModalProps) {
  const [sql, setSql] = useState(request.sql);
  const [values, setValues] = useState(request.values);
  const [tried, setTried] = useState(missing.length > 0);
  const [samplesAllowed, setSamplesAllowed] = useState<boolean | null>(null);
  const [schemaName, setSchemaName] = useState<string | null>(null);
  const max = config.dialogs.message_max_chars;
  const parameters = sqlParameters(sql);
  const withDb = config.db.enabled && Boolean(project.db?.available);
  const hasSchema = project.schema_id !== null || withDb;

  useEffect(() => {
    if (withDb) {
      api.listDbConnections().then(
        ({ items }) => setSamplesAllowed(items.find((item) => item.id === project.db?.connection_id)?.samples_allowed ?? false),
        () => setSamplesAllowed(false),
      );
    } else if (project.schema_id) {
      api.listSqlSchemas().then(
        ({ items }) => setSchemaName(items.find((item) => item.id === project.schema_id)?.name ?? null),
        () => {
          // Без названия строка «как проверю» говорит о схеме без имени.
        },
      );
    }
  }, [project.db?.connection_id, project.schema_id, withDb]);

  const sqlError = sql.trim() === '' ? c.enterQuery : sql.length > max ? c.queryTooLong(max) : null;
  const valueError = (name: string) => (tried && (values[name] ?? '') === '') || missing.includes(name);
  const how = withDb
    ? samplesAllowed
      ? c.howSample(project.db?.title ?? '')
      : c.howInvented(project.db?.title ?? '')
    : c.howInvented(schemaName ?? '');

  const submit = () => {
    setTried(true);
    if (sqlError || parameters.some((name) => (values[name] ?? '') === '')) {
      return;
    }
    onSubmit({ ...request, sql, values: Object.fromEntries(parameters.map((name) => [name, values[name] ?? ''])), edited: sql !== request.original });
  };

  return (
    <Modal width={640} ignoreBackgroundClick onClose={onClose}>
      <Modal.Header>{c.title}</Modal.Header>
      <Modal.Body>
        {!hasSchema ? (
          <Notice kind="info" action={{ label: c.settings, onClick: onOpenSettings }}>
            {c.noSchema}
          </Notice>
        ) : (
          <div className={styles.body}>
            <div className={styles.field}>
              <label htmlFor="sql-check-query" className={styles.label}>
                {c.query}
              </label>
              <Textarea
                id="sql-check-query"
                className={styles.mono}
                width="100%"
                autoResize
                rows={4}
                maxRows={16}
                value={sql}
                error={tried && sqlError !== null}
                onValueChange={setSql}
              />
              {tried && sqlError ? (
                <p className={styles.error} role="alert">
                  {sqlError}
                </p>
              ) : (
                <p className={styles.muted}>{c.queryNote}</p>
              )}
            </div>
            {parameters.length > 0 && (
              <fieldset className={styles.params}>
                <legend className={styles.label}>{c.params}</legend>
                {parameters.map((name) => (
                  <div key={name} className={styles.param}>
                    <label htmlFor={`sql-check-${name}`} className={styles.paramName}>
                      {name}
                    </label>
                    <div>
                      <Input
                        id={`sql-check-${name}`}
                        width={320}
                        aria-label={c.paramLabel(name)}
                        value={values[name] ?? ''}
                        error={valueError(name)}
                        onValueChange={(value) => setValues((current) => ({ ...current, [name]: value }))}
                      />
                      {valueError(name) && (
                        <p className={styles.error} role="alert">
                          {c.enterValue}
                        </p>
                      )}
                    </div>
                  </div>
                ))}
                <p className={styles.muted}>{c.paramsNote}</p>
                {parameters.some((name) => name.startsWith('@')) && <p className={styles.muted}>{c.atNote}</p>}
              </fieldset>
            )}
            <p className={styles.muted}>{how}</p>
          </div>
        )}
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          {hasSchema && (
            <Button use="primary" onClick={submit}>
              {c.submit}
            </Button>
          )}
          <Button onClick={onClose}>{texts.common.cancel}</Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}

interface Options {
  project: CodeProject | null;
  config: PortalConfig | null;
  sectionOff: (error: unknown) => boolean;
  reloadConfig: () => void;
  onOpenSettings: () => void;
  /** Итог появился — панель внизу разворачивается на вкладке «Проверка запроса». */
  onShowResult: () => void;
}

interface Result {
  request: Request;
  block: SqlRunBlock;
  running: boolean;
  failure: string | null;
  /** «Проверка недоступна: …» — отказ подключения, однострочное состояние (§5.13.24). */
  unavailable?: string | null;
}

/** «Проверить запрос» в проекте (§5.13.23): окно, поток проверки и вкладка с итогом. Итог не сохраняется. */
export function useSqlCheck({ project, config, sectionOff, reloadConfig, onOpenSettings, onShowResult }: Options) {
  const [modal, setModal] = useState<{ request: Request; missing: string[] } | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const controller = useRef<AbortController | null>(null);
  const enabled = config?.sql.test_run.enabled === true;
  // Окно открывают из меню редактора: закрытие без проверки возвращает фокус в редактор, запуск — на итог (§9).
  const rememberOpener = useFocusReturn(modal !== null);
  const [focusResult, setFocusResult] = useState(false);

  // Уход из проекта останавливает проверку — итога нет.
  useEffect(() => () => controller.current?.abort(), []);

  const run = useCallback(
    async (request: Request) => {
      if (!project) {
        return;
      }
      if (controller.current) {
        SingleToast.push(c.busy);
        return;
      }
      const abort = new AbortController();
      controller.current = abort;
      setResult({ request, block: PENDING, running: true, failure: null });
      setFocusResult(true);
      onShowResult();
      const parameters = Object.entries(request.values).map(([name, value]) => ({ name, value }));
      try {
        for await (const event of api.checkProjectSql(project.id, { sql: request.sql, parameters }, abort.signal)) {
          if (event.type === 'sql_run') {
            const block = event.blocks[0];
            if (block) {
              setResult((current) => (current ? { ...current, block } : current));
            }
          } else if (event.type === 'error') {
            setResult((current) => (current ? { ...current, failure: event.message } : current));
          }
        }
        setResult((current) => (current ? { ...current, running: false } : current));
      } catch (error) {
        if (abort.signal.aborted) {
          setResult((current) => (current ? { ...current, running: false, block: { ...current.block, status: 'stopped' } } : current));
          return;
        }
        setResult(null);
        if (isApiError(error, 'sql_check_in_progress')) {
          SingleToast.push(c.busy);
        } else if (isApiError(error, 'feature_disabled')) {
          SingleToast.push(c.off);
          reloadConfig();
        } else if (isApiError(error, 'validation_error') && error.fields.some((field) => field.field === 'parameters')) {
          const field = error.fields.find((item) => item.field === 'parameters');
          if (field?.code === 'unknown_value') {
            SingleToast.push(c.paramsChanged);
            setModal({ request, missing: [] });
          } else {
            const names = Array.isArray(error.details.names) ? error.details.names.map(String) : [];
            setModal({ request, missing: names });
          }
        } else if (isApiError(error, 'schema_required')) {
          setModal({ request, missing: [] });
        } else if (!sectionOff(error) && !isApiError(error, 'unauthenticated')) {
          const unavailable = dbCheckUnavailable(error);
          setResult({ request, block: PENDING, running: false, failure: unavailable ? null : (dbRefusal(error) ?? texts.common.actionFailed), unavailable });
        }
      } finally {
        if (controller.current === abort) {
          controller.current = null;
        }
      }
    },
    [onShowResult, project, reloadConfig, sectionOff],
  );

  /** Пункт меню редактора: выделенный текст и место (§5.13.23). */
  const open = useCallback((text: string, source: SqlSource) => {
    const sql = stripQuotes(text);
    rememberOpener(EDITOR);
    setModal({ request: { sql, values: {}, source, edited: false, original: sql }, missing: [] });
  }, [rememberOpener]);

  const element =
    // Проверку выключили, пока окно открыто, — окно закрывается (§5.13.24).
    modal && enabled && project && config ? (
      <SqlCheckModal
        request={modal.request}
        project={project}
        config={config}
        missing={modal.missing}
        onClose={() => setModal(null)}
        onOpenSettings={() => {
          setModal(null);
          onOpenSettings();
        }}
        onSubmit={(request) => {
          // Фокус уходит на итог, а не в редактор.
          rememberOpener('');
          setModal(null);
          void run(request);
        }}
      />
    ) : null;

  const tab = result ? (
    <SqlCheckResult
      result={result}
      focusTitle={focusResult}
      onFocused={() => setFocusResult(false)}
      onAgain={() => void run(result.request)}
      onEdit={() => setModal({ request: result.request, missing: [] })}
      onStop={() => controller.current?.abort()}
    />
  ) : null;

  return { enabled, open, modal: element, tab };
}

function parametersLine(values: Record<string, string>): string {
  const line = Object.entries(values)
    .map(([name, value]) => `${name} = ${value}`)
    .join(', ');
  return line.length > 120 ? `${line.slice(0, 119)}…` : line;
}

/** Вкладка «Проверка запроса» панели внизу (§5.13.23): последняя проверка, пока открыта страница. */
function SqlCheckResult({
  result,
  focusTitle,
  onFocused,
  onAgain,
  onEdit,
  onStop,
}: {
  result: Result;
  /** Запуск проверки: фокус — на заголовок итога, один раз (§9). */
  focusTitle: boolean;
  onFocused: () => void;
  onAgain: () => void;
  onEdit: () => void;
  onStop: () => void;
}) {
  const { request, block, running, failure, unavailable } = result;
  const title = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (focusTitle && title.current) {
      title.current.focus();
      onFocused();
    }
  }, [focusTitle, onFocused]);
  const announcement = running ? '' : (unavailable ?? failure ?? sqlRunAnnouncement({ blocks: [block] }));
  const head = [c.head, c.lines(request.source.path, request.source.startLine, request.source.endLine), ...(request.edited ? [c.edited] : [])];
  const values = parametersLine(request.values);
  return (
    <div className={styles.result}>
      <div className={styles.resultHead}>
        <p className={styles.resultTitle} ref={title} tabIndex={-1}>
          {head.join(' · ')}
          {values && <span className={styles.mono}> · {values}</span>}
        </p>
        {running ? (
          <Link component="button" onClick={onStop}>
            {c.stop}
          </Link>
        ) : (
          <Gapped gap={12}>
            <Button size="small" onClick={onAgain}>
              {c.again}
            </Button>
            <Link component="button" onClick={onEdit}>
              {c.edit}
            </Link>
          </Gapped>
        )}
      </div>
      {unavailable ? (
        <Line icon={<IconInfoCircleRegular16 />}>{unavailable}</Line>
      ) : failure ? (
        <Notice kind="error">{failure}</Notice>
      ) : (
        <SqlRunResult block={block} />
      )}
      {block.status === 'fixed' && <p className={styles.muted}>{c.fixedNote}</p>}
      <p className={styles.muted}>{c.notSaved}</p>
      <div className="p-visually-hidden" role="status">
        {announcement}
      </div>
    </div>
  );
}
