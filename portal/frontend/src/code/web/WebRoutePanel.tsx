import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCUpRegular16 } from '@skbkontur/icons/IconArrowCUpRegular16';
import { IconArrowRoundSyncForwardRegular16 } from '@skbkontur/icons/IconArrowRoundSyncForwardRegular16';
import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconMediaUiAPlayRegular16 } from '@skbkontur/icons/IconMediaUiAPlayRegular16';
import { IconNetDownloadRegular16 } from '@skbkontur/icons/IconNetDownloadRegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconXCircleRegular16 } from '@skbkontur/icons/IconXCircleRegular16';
import { Button, Hint, ScrollContainer, Tabs } from '@skbkontur/react-ui';

import type { CodeWeb } from '../../api/types';
import { texts } from '../../texts';
import styles from '../BuildPanel.module.css';
import { MessageRow } from '../BuildPanel';
import type { SyntaxMessage } from './webRules';

const r = texts.workshop.web.route;
const b = texts.workshop.build;

export type WebTab = 'syntax' | 'sql';

/** Станция «Предпросмотр» (§5.13.20): не запускался, без ошибок, с ошибками, код изменён после запуска. */
export type PreviewStation = 'none' | 'ok' | 'errors' | 'changed';

export type WebPrimary = 'fixSyntax' | 'preview' | 'fixPreview' | 'export';

/** Следующий шаг — ровно одна основная кнопка в строке (§5.13.20). */
export function webPrimary(syntaxErrors: number, previewEnabled: boolean, preview: PreviewStation): WebPrimary {
  if (syntaxErrors > 0) {
    return 'fixSyntax';
  }
  if (!previewEnabled) {
    return 'export';
  }
  if (preview === 'errors') {
    return 'fixPreview';
  }
  return preview === 'ok' ? 'export' : 'preview';
}

/** Строка `role="status"` (§5.13.20). */
export function webAnnouncement(syntax: SyntaxMessage[] | null, previewEnabled: boolean, preview: PreviewStation, previewErrors: number): string {
  if (syntax === null) {
    return '';
  }
  if (syntax.length > 0) {
    return r.announce.syntax(syntax.length);
  }
  if (previewEnabled && preview === 'errors') {
    return r.announce.preview(previewErrors);
  }
  return previewEnabled && preview === 'ok' ? r.announce.ok : r.announce.syntaxOk;
}

interface Props {
  /** `null` — первая проверка ещё идёт. */
  syntax: SyntaxMessage[] | null;
  previewEnabled: boolean;
  preview: PreviewStation;
  previewErrors: number;
  previewOpen: boolean;
  expanded: boolean;
  tab: WebTab;
  hasHtml: boolean;
  libraries: CodeWeb['libraries'] | null;
  /** Вкладка «Проверка запроса» — после первой проверки (§5.13.23). */
  sqlTab: React.ReactNode | null;
  paths: ReadonlySet<string>;
  onToggle: (tab?: WebTab) => void;
  onTab: (tab: WebTab) => void;
  onFixSyntax: () => void;
  onFixPreview: () => void;
  onRunPreview: () => void;
  onShowPreview: () => void;
  onExport: () => void;
  onJump: (path: string, line: number, column: number) => void;
}

function Muted({ children }: { children: React.ReactNode }) {
  return <span className={styles.muted}>{children}</span>;
}

function librariesLine(libraries: CodeWeb['libraries']): string {
  const found = libraries.filter((library) => library.available);
  if (found.length === 0) {
    return r.noLibraries;
  }
  return r.libraries(found.map((library) => `${r.libraryNames[library.id]}${library.version ? ` ${library.version}` : ''}`).join(', '));
}

/** «Путь до CoGIS» для JS и HTML (концепция §5.13.20): Синтаксис › Предпросмотр › Скачать для CoGIS. */
export function WebRoutePanel(props: Props) {
  const { syntax, previewEnabled, preview, previewErrors, expanded, tab } = props;
  const errors = syntax?.length ?? 0;
  const primary = webPrimary(errors, previewEnabled, preview);

  let syntaxStation: React.ReactNode;
  if (syntax === null) {
    syntaxStation = <Muted>{r.syntax}</Muted>;
  } else if (errors > 0) {
    syntaxStation = (
      <>
        <Hint text={r.syntaxSpoken(errors)}>
          <button type="button" className={styles.station} aria-label={r.syntaxButton(r.syntaxSpoken(errors))} onClick={() => props.onToggle('syntax')}>
            <IconXCircleRegular16 className={styles.error} />
            {r.syntaxErrors(errors)}
          </button>
        </Hint>
        <Button size="small" use={primary === 'fixSyntax' ? 'primary' : 'default'} onClick={props.onFixSyntax}>
          {b.fix}
        </Button>
      </>
    );
  } else {
    syntaxStation = (
      <button type="button" className={styles.station} aria-label={r.syntaxButton(r.noErrorsSpoken)} onClick={() => props.onToggle('syntax')}>
        <IconCheckARegular16 className={styles.ok} />
        {r.syntax}
      </button>
    );
  }

  let previewStation: React.ReactNode = null;
  if (previewEnabled) {
    if (errors > 0 || syntax === null) {
      previewStation = <Muted>{r.preview}</Muted>;
    } else if (preview === 'errors') {
      previewStation = (
        <>
          <Hint text={r.syntaxSpoken(previewErrors)}>
            <button type="button" className={styles.station} aria-label={r.previewButton(r.syntaxSpoken(previewErrors))} onClick={props.onShowPreview}>
              <IconXCircleRegular16 className={styles.error} />
              {r.previewErrors(previewErrors)}
            </button>
          </Hint>
          <Button size="small" use={primary === 'fixPreview' ? 'primary' : 'default'} onClick={props.onFixPreview}>
            {b.fix}
          </Button>
        </>
      );
    } else if (preview === 'ok') {
      previewStation = (
        <button type="button" className={styles.station} aria-label={r.previewButton(r.noErrorsSpoken)} onClick={props.onShowPreview}>
          <IconCheckARegular16 className={styles.ok} />
          {r.preview}
        </button>
      );
    } else if (preview === 'changed') {
      previewStation = (
        <>
          <Hint text={r.changedHint}>
            <span className={styles.stationText} tabIndex={0}>
              <IconTimeClockRegular16 className={styles.mutedIcon} />
              {r.changed}
            </span>
          </Hint>
          <Button size="small" use={primary === 'preview' ? 'primary' : 'default'} icon={<IconArrowRoundSyncForwardRegular16 />} onClick={props.onRunPreview}>
            {r.rerun}
          </Button>
        </>
      );
    } else {
      previewStation = (
        <Button
          size="small"
          use={primary === 'preview' ? 'primary' : 'default'}
          icon={props.previewOpen ? <IconArrowRoundSyncForwardRegular16 /> : <IconMediaUiAPlayRegular16 />}
          onClick={props.onRunPreview}
        >
          {props.previewOpen ? r.rerun : r.open}
        </Button>
      );
    }
  }

  const list = syntax ?? [];
  const content =
    tab === 'sql' && props.sqlTab ? (
      props.sqlTab
    ) : (
      <>
        {props.hasHtml && <p className={styles.note}>{r.htmlNotChecked}</p>}
        {list.length > 0 ? (
          <ul className={styles.list}>
            {list.map((item, index) => (
              <MessageRow
                key={index}
                kind="error"
                file={item.path}
                line={item.line}
                column={item.column}
                text={item.message}
                exists={props.paths.has(item.path)}
                onJump={props.onJump}
              />
            ))}
          </ul>
        ) : (
          <p className={styles.note}>{r.noSyntaxErrors}</p>
        )}
      </>
    );

  return (
    <section aria-label={r.region} className={expanded ? `${styles.panel} ${styles.expanded}` : styles.panel}>
      <div className={styles.route}>
        <ol className={styles.stations} aria-label={b.route}>
          <li>{syntaxStation}</li>
          {previewStation && (
            <>
              <li aria-hidden="true" className={styles.sep}>
                ›
              </li>
              <li>{previewStation}</li>
            </>
          )}
          <li aria-hidden="true" className={styles.sep}>
            ›
          </li>
          <li data-opener="web-export">
            <Button size="small" use={primary === 'export' ? 'primary' : 'default'} icon={<IconNetDownloadRegular16 />} onClick={props.onExport}>
              {texts.workshop.web.exportCogis}
            </Button>
          </li>
        </ol>
        <span className="p-visually-hidden" role="status">
          {webAnnouncement(syntax, previewEnabled, preview, previewErrors)}
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
        <div className={styles.messages}>
          <div className={styles.tabs}>
            <Tabs<WebTab> value={props.sqlTab ? tab : 'syntax'} onValueChange={props.onTab} size="small">
              <Tabs.Tab id="syntax">{r.tabs.syntax(errors)}</Tabs.Tab>
              {props.sqlTab && <Tabs.Tab id="sql">{r.tabs.sqlCheck}</Tabs.Tab>}
            </Tabs>
          </div>
          <div className={styles.listScroll}>
            <ScrollContainer>{content}</ScrollContainer>
          </div>
          {props.libraries && <p className={styles.footer}>{librariesLine(props.libraries)}</p>}
        </div>
      )}
    </section>
  );
}
