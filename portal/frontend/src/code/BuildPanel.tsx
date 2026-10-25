import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCUpRegular16 } from '@skbkontur/icons/IconArrowCUpRegular16';
import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconInfoCircleRegular16 } from '@skbkontur/icons/IconInfoCircleRegular16';
import { IconNetDownloadRegular16 } from '@skbkontur/icons/IconNetDownloadRegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconToolHardHammersRegular16 } from '@skbkontur/icons/IconToolHardHammersRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Hint, Link, ScrollContainer, Spinner, Tabs } from '@skbkontur/react-ui';

import type { CodeBuild, Diagnostic, RuleFinding } from '../api/types';
import { texts } from '../texts';
import styles from './BuildPanel.module.css';
import { formatDateTime, primaryAction, routeAnnouncement, type RouteState } from './rules';

const b = texts.workshop.build;

export type BuildTab = 'errors' | 'warnings' | 'rules';

/** Вкладка, открытая при развороте: первая непустая (§5.13.8). */
export function firstTab(build: CodeBuild | null): BuildTab {
  if (!build) {
    return 'errors';
  }
  if (build.diagnostics.some((item) => item.severity === 'error')) {
    return 'errors';
  }
  if (build.diagnostics.some((item) => item.severity === 'warning')) {
    return 'warnings';
  }
  return (build.findings?.length ?? 0) > 0 ? 'rules' : 'errors';
}

/** Есть ли что показать сразу: панель разворачивается сама при ошибках, нарушениях и предупреждениях. */
export function hasMessages(build: CodeBuild): boolean {
  return build.diagnostics.length > 0 || (build.findings?.length ?? 0) > 0;
}

interface Props {
  state: RouteState;
  build: CodeBuild | null;
  expanded: boolean;
  tab: BuildTab;
  /** Пути файлов проекта: сообщение к удалённому файлу — без перехода. */
  paths: ReadonlySet<string>;
  onToggle: (tab?: BuildTab) => void;
  onTab: (tab: BuildTab) => void;
  onBuild: () => void;
  onFix: () => void;
  onDownload: () => void;
  onExportVs: () => void;
  onJump: (path: string, line: number, column: number) => void;
}

function Muted({ children }: { children: React.ReactNode }) {
  return <span className={styles.muted}>{children}</span>;
}

/** Станции «Пути до CoGIS» по итогу (таблица §5.13.8). */
function Stations({ state, onToggle, onFix, onDownload, fixPrimary }: Pick<Props, 'state' | 'onToggle' | 'onFix' | 'onDownload'> & { fixPrimary: boolean }) {
  const fix = (
    <Button size="small" use={fixPrimary ? 'primary' : 'default'} onClick={onFix}>
      {b.fix}
    </Button>
  );
  const station = (content: React.ReactNode, label: string | null, tab: BuildTab, className?: string) =>
    label ? (
      <button type="button" className={`${styles.station} ${className ?? ''}`} aria-label={label} onClick={() => onToggle(tab)}>
        {content}
      </button>
    ) : (
      <span className={`${styles.stationText} ${className ?? ''}`}>{content}</span>
    );

  let first: React.ReactNode;
  let rules: React.ReactNode = <Muted>{b.stationRules}</Muted>;
  let archive: React.ReactNode = <Muted>{b.stationArchive}</Muted>;
  switch (state.kind) {
    case 'none':
      first = <Muted>{b.stationBuild}</Muted>;
      break;
    case 'building':
      first = (
        <span className={styles.stationText}>
          <Spinner type="mini" caption={b.building} />
        </span>
      );
      break;
    case 'failed':
      first = (
        <>
          <Hint text={b.failedSpoken(state.errors)}>
            {station(
              <>
                <IconXCircleRegular16 className={styles.error} />
                {b.failed(state.errors)}
              </>,
              b.stationButton(b.failedSpoken(state.errors)),
              'errors',
            )}
          </Hint>
          {fix}
        </>
      );
      break;
    case 'findings':
      first = station(
        <>
          <IconCheckARegular16 className={styles.ok} />
          {b.built}
        </>,
        b.stationButton(b.built),
        'errors',
      );
      rules = (
        <>
          <Hint text={b.rulesSpoken(state.findings)}>
            {station(
              <>
                <IconXCircleRegular16 className={styles.error} />
                {b.rules(state.findings)}
              </>,
              b.rulesButton(b.rulesSpoken(state.findings)),
              'rules',
            )}
          </Hint>
          {fix}
        </>
      );
      break;
    case 'ready':
      first = station(
        <>
          <IconCheckARegular16 className={styles.ok} />
          {b.built}
        </>,
        b.stationButton(b.built),
        'errors',
      );
      rules = station(
        <>
          <IconCheckARegular16 className={styles.ok} />
          {b.stationRules}
        </>,
        b.rulesButton(b.noFindings),
        'rules',
      );
      archive = (
        <Button size="small" use="primary" icon={<IconNetDownloadRegular16 />} onClick={onDownload}>
          {b.download}
        </Button>
      );
      break;
    case 'outdated':
      first = (
        <Hint text={b.outdatedHint}>
          {station(
            <>
              <IconTimeClockRegular16 className={styles.mutedIcon} />
              {b.outdated}
            </>,
            b.stationButton(b.outdated),
            firstTab(state.build),
          )}
        </Hint>
      );
      rules = <Muted>{state.build.findings && state.build.findings.length > 0 ? b.rules(state.build.findings.length) : b.stationRules}</Muted>;
      break;
    case 'timeout':
      first = station(
        <>
          <IconXCircleRegular16 className={styles.error} />
          {b.timeout}
        </>,
        null,
        'errors',
      );
      break;
    case 'unavailable':
      first = (
        <Hint text={b.unavailableHint}>
          <span className={styles.stationText} tabIndex={0}>
            <IconInfoCircleRegular16 className={styles.mutedIcon} />
            {b.unavailable}
          </span>
        </Hint>
      );
      break;
    case 'disabled':
      return null;
  }
  return (
    <ol className={styles.stations} aria-label={b.route}>
      <li>{first}</li>
      <li aria-hidden="true" className={styles.sep}>
        ›
      </li>
      <li>{rules}</li>
      <li aria-hidden="true" className={styles.sep}>
        ›
      </li>
      <li data-opener="archive">{archive}</li>
    </ol>
  );
}

interface MessageRowProps {
  kind: 'error' | 'warning' | 'rule';
  file: string | null;
  line: number | null;
  column: number | null;
  text: string;
  exists: boolean;
  onJump: Props['onJump'];
}

/** Строка сообщения — кнопка во всю ширину: переход к месту в редакторе (§5.13.8, §9). */
function MessageRow({ kind, file, line, column, text, exists, onJump }: MessageRowProps) {
  const icon =
    kind === 'warning' ? <IconWarningTriangleRegular16 className={styles.warning} /> : <IconXCircleRegular16 className={styles.error} />;
  const place = file && line ? b.place(file, line) : file;
  if (!file || !line || !exists) {
    return (
      <li className={styles.message}>
        <span className={styles.messageInner}>
          {icon}
          {place && (
            <span className={styles.placeText}>
              <span className={styles.mono}>{place}</span>
              {file && !exists && <Muted> · {b.fileGone}</Muted>}
            </span>
          )}
          <span className={styles.text}>{text}</span>
        </span>
      </li>
    );
  }
  return (
    <li className={styles.message}>
      <button
        type="button"
        className={`${styles.messageInner} ${styles.messageButton}`}
        aria-label={`${b.messageLabel[kind]}: ${place}: ${text}`}
        onClick={() => onJump(file, line, column ?? 1)}
      >
        {icon}
        <span className={styles.place}>
          <span className={styles.mono}>{place}</span>
        </span>
        <span className={styles.text}>{text}</span>
      </button>
    </li>
  );
}

function diagnosticsList(items: Diagnostic[], kind: 'error' | 'warning', paths: ReadonlySet<string>, onJump: Props['onJump']) {
  return items.map((item, index) => (
    <MessageRow
      key={index}
      kind={kind}
      file={item.file}
      line={item.line}
      column={item.column}
      text={item.message}
      exists={item.file !== null && paths.has(item.file)}
      onJump={onJump}
    />
  ));
}

function findingsList(items: RuleFinding[], paths: ReadonlySet<string>, onJump: Props['onJump']) {
  return items.map((item, index) => (
    <MessageRow
      key={index}
      kind="rule"
      file={item.file}
      line={item.line}
      column={item.column}
      text={item.message}
      exists={item.file !== null && paths.has(item.file)}
      onJump={onJump}
    />
  ));
}

/** Сообщения последней сборки по вкладкам (§5.13.8). */
function Messages({ build, tab, paths, outdated, onTab, onJump }: { build: CodeBuild | null; tab: BuildTab; paths: ReadonlySet<string>; outdated: boolean; onTab: (tab: BuildTab) => void; onJump: Props['onJump'] }) {
  const errors = build?.diagnostics.filter((item) => item.severity === 'error') ?? [];
  const warnings = build?.diagnostics.filter((item) => item.severity === 'warning') ?? [];
  const findings = build?.findings ?? null;
  let content: React.ReactNode;
  if (tab === 'errors') {
    content = errors.length > 0 ? <ul className={styles.list}>{diagnosticsList(errors, 'error', paths, onJump)}</ul> : <p className={styles.note}>{b.noErrors}</p>;
  } else if (tab === 'warnings') {
    content = (
      <>
        <p className={styles.note}>{b.warningsNote}</p>
        {warnings.length > 0 ? <ul className={styles.list}>{diagnosticsList(warnings, 'warning', paths, onJump)}</ul> : <p className={styles.note}>{b.noWarnings}</p>}
      </>
    );
  } else if (findings === null) {
    content = <p className={styles.note}>{b.rulesAfterBuild}</p>;
  } else {
    content = (
      <>
        <p className={styles.note}>{b.rulesNote}</p>
        {findings.length > 0 ? <ul className={styles.list}>{findingsList(findings, paths, onJump)}</ul> : <p className={styles.note}>{b.noFindings}</p>}
      </>
    );
  }
  const footer = build
    ? [
        b.footer.built(formatDateTime(build.created_at)),
        b.footer.dotnet,
        ...(build.sdk_version ? [b.footer.sdk(build.sdk_version)] : []),
        ...(build.packages.length > 0 ? [b.footer.packages(build.packages.map((item) => `${item.id} ${item.version}`).join(', '))] : []),
        b.footer.notRun,
      ].join(' · ')
    : null;
  return (
    <div className={styles.messages}>
      {outdated && <p className={styles.stale}>{b.staleMessages}</p>}
      <div className={styles.tabs}>
        <Tabs<BuildTab> value={tab} onValueChange={onTab} size="small">
          <Tabs.Tab id="errors">{b.tabs.errors(errors.length)}</Tabs.Tab>
          <Tabs.Tab id="warnings">{b.tabs.warnings(warnings.length)}</Tabs.Tab>
          <Tabs.Tab id="rules">{b.tabs.rules(findings?.length ?? 0)}</Tabs.Tab>
        </Tabs>
      </div>
      <div className={styles.listScroll}>
        <ScrollContainer>{content}</ScrollContainer>
      </div>
      {footer && <p className={styles.footer}>{footer}</p>}
    </div>
  );
}

/** Панель «Сборка»: строка «Путь до CoGIS» и сообщения последней сборки (концепция §5.13.8). */
export function BuildPanel(props: Props) {
  const { state, build, expanded } = props;
  const primary = primaryAction(state);
  if (state.kind === 'disabled') {
    return (
      <section aria-label={b.region} className={styles.panel}>
        <div className={styles.route}>
          <span className={styles.disabled}>{b.disabled}</span>
          <Link component="button" onClick={props.onExportVs}>{texts.workshop.exportVs}</Link>
        </div>
      </section>
    );
  }
  return (
    <section aria-label={b.region} className={expanded ? `${styles.panel} ${styles.expanded}` : styles.panel}>
      <div className={styles.route}>
        <Button
          size="small"
          use={primary === 'build' ? 'primary' : 'default'}
          loading={state.kind === 'building'}
          icon={<IconToolHardHammersRegular16 />}
          onClick={props.onBuild}
        >
          {b.build}
        </Button>
        <Stations state={state} onToggle={props.onToggle} onFix={props.onFix} onDownload={props.onDownload} fixPrimary={primary === 'fix'} />
        <span className="p-visually-hidden" role="status">
          {routeAnnouncement(state)}
        </span>
        <span className={styles.toggle}>
          <Hint text={expanded ? b.hideMessages : b.showMessages} pos="left">
            <Button
              size="small"
              use="text"
              aria-label={expanded ? b.hideMessages : b.showMessages}
              aria-expanded={expanded}
              icon={expanded ? <IconArrowCDownRegular16 /> : <IconArrowCUpRegular16 />}
              onClick={() => props.onToggle()}
            />
          </Hint>
        </span>
      </div>
      {expanded && (
        <Messages
          build={build}
          tab={props.tab}
          paths={props.paths}
          outdated={state.kind === 'outdated'}
          onTab={props.onTab}
          onJump={props.onJump}
        />
      )}
    </section>
  );
}
