import { IconArrowCLeftRegular16 } from '@skbkontur/icons/IconArrowCLeftRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { IconMediaUiAPlayRegular16 } from '@skbkontur/icons/IconMediaUiAPlayRegular16';
import {
  Button,
  Link,
  Loader,
  SidePage,
  SidePageDataTids,
  SidePageHeaderDataTids,
  SingleToast,
} from '@skbkontur/react-ui';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { api, isApiError, kbAudioUrl, kbFileUrl, kbImageUrl } from '../../api/client';
import type { DocumentText, KbDocument } from '../../api/types';
import { useFocusReturn } from '../../hooks/useFocusReturn';
import { formatDate, formatDuration, formatTimecode, shortName, spokenTimecode, texts } from '../../texts';
import { Notice } from '../Notice';
import { AudioPlayer } from './AudioPlayer';
import styles from './DocumentViewer.module.css';

const t = texts.viewer;
const WIDTH = 560;

export interface ViewerTarget {
  documentId: string;
  /** Название до ответа сервера: из карточки источника или строки списка. */
  title: string;
  /** Цитата: сервер подсветит её отрезки на её странице. */
  fragmentId?: string;
  /** Страница из карточки источника: по ней документ открывается на прежнем месте и после повторной обработки. */
  page?: number;
  /** Момент записи из карточки источника, в секундах, — то же для записи, у которой страниц нет. */
  timecode?: number;
  /** Документ из списка базы знаний; из карточки источника сведения запрашиваются отдельно. */
  document?: KbDocument;
}

interface Opened {
  target: ViewerTarget;
  /** Номер открытия: у нового источника проигрыватель и его состояние начинаются заново. */
  openId: number;
  /** Сведения для шапки; `null` — не получены: шапка остаётся с одним названием. */
  document: KbDocument | null;
  text: DocumentText | null;
  loading: boolean;
  /** Страница, которую не удалось открыть при листании. */
  failedPage: number | null;
}

type Segment = DocumentText['segments'][number];

/**
 * Просмотр документа рядом с ответом (концепция §4.4). Панель открывается после первого ответа
 * сервера: удалённый или необработанный документ её не открывает, а открытую — закрывает.
 */
export function useDocumentViewer() {
  const [opened, setOpened] = useState<Opened | null>(null);
  const [opening, setOpening] = useState<ViewerTarget | null>(null);
  const request = useRef(0);
  const rememberOpener = useFocusReturn(opened !== null);

  /** `current` — уже открытая панель (листание на страницу `page`); `null` — первое открытие. */
  const load = useCallback(async (target: ViewerTarget, page: number | undefined, current: Opened | null) => {
    const id = (request.current += 1);
    if (current) {
      setOpened({ ...current, loading: true, failedPage: null });
    } else {
      setOpening(target);
    }
    // Цитата передаётся и при листании: сервер подсветит её, когда она на запрошенной странице.
    // Момент записи — только при открытии: при листании часть задаёт её номер (контракт §8.3).
    const where = { page, fragmentId: target.fragmentId, timecode: current ? undefined : target.timecode };
    const known = current?.document ?? target.document ?? null;
    const meta = known ? Promise.resolve(known) : api.getKbDocument(target.documentId).catch(() => null);
    try {
      const [text, document] = await Promise.all([api.getKbDocumentText(target.documentId, where), meta]);
      if (id === request.current) {
        setOpened({ target, openId: current?.openId ?? id, document, text, loading: false, failedPage: null });
      }
    } catch (error) {
      if (id !== request.current) {
        return;
      }
      if (isApiError(error, 'not_found') || isApiError(error, 'document_not_ready')) {
        SingleToast.push(isApiError(error, 'not_found') ? t.deleted : t.notReady, { use: 'error' });
        setOpened(null);
      } else if (isApiError(error, 'unauthenticated')) {
        setOpened(null);
      } else if (current && page !== undefined) {
        // Сбой при листании: панель остаётся с прежней страницей и заметкой.
        setOpened({ ...current, loading: false, failedPage: page });
      } else {
        // Сбой при первом открытии: панель не появляется.
        SingleToast.push(t.failed, { use: 'error' });
      }
    } finally {
      if (id === request.current) {
        setOpening(null);
      }
    }
  }, []);

  /** `openerSelector` — куда вернуть фокус после закрытия панели. */
  const open = useCallback(
    (target: ViewerTarget, openerSelector: string) => {
      rememberOpener(openerSelector);
      void load(target, target.page, null);
    },
    [load, rememberOpener],
  );

  const close = useCallback(() => {
    request.current += 1;
    setOpening(null);
    setOpened(null);
  }, []);

  const element = opened ? (
    <DocumentViewer
      opened={opened}
      onPage={(page) => void load(opened.target, page, opened)}
      onClose={close}
    />
  ) : null;

  /** `opening` — что открывается прямо сейчас: на нажатой карточке или строке показывается индикатор. */
  return { open, close, isOpen: opened !== null, opening, element };
}

/** Первый подсвеченный отрезок прокручивается в видимую часть при открытии страницы. */
function useScrollToQuote(text: DocumentText | null) {
  const firstMark = useRef<HTMLElement>(null);
  useEffect(() => {
    firstMark.current?.scrollIntoView({ block: 'center' });
  }, [text]);
  return firstMark;
}

/** Текст отрезками (контракт §8.3): цитата — на подложке; смещений интерфейс не считает. */
function Segments({ segments, firstMark }: { segments: Segment[]; firstMark: React.Ref<HTMLElement> }) {
  const first = segments.find((segment) => segment.highlight);
  return segments.map((segment, index) =>
    segment.highlight ? (
      <mark key={index} ref={segment === first ? firstMark : undefined} className={styles.mark}>
        {segment.text}
      </mark>
    ) : (
      segment.text
    ),
  );
}

/**
 * Высота закреплённой шапки панели: проигрыватель записи встаёт сразу под ней и не уезжает при прокрутке
 * расшифровки. Своего способа закрепить часть тела у `SidePage` нет, а содержимое шапки библиотека
 * пересоздаёт при прокрутке — звук в ней прерывался бы.
 */
function useHeaderHeight(inside: HTMLElement | null): number {
  const [height, setHeight] = useState(0);
  useLayoutEffect(() => {
    const header = inside
      ?.closest(`[data-tid="${SidePageDataTids.container}"]`)
      ?.querySelector<HTMLElement>(`[data-tid="${SidePageHeaderDataTids.root}"]`);
    if (!header || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => setHeight(header.offsetHeight));
    observer.observe(header);
    return () => observer.disconnect();
  }, [inside]);
  return height;
}

interface PagerProps {
  page: number;
  count: number;
  label: string;
  previous: string;
  next: string;
  onPage: (page: number) => void;
}

function Pager({ page, count, label, previous, next, onPage }: PagerProps) {
  return (
    <div className={styles.pages}>
      <Button icon={<IconArrowCLeftRegular16 />} aria-label={previous} disabled={page <= 1} onClick={() => onPage(page - 1)} />
      <span>{label}</span>
      <Button icon={<IconArrowCRightRegular16 />} aria-label={next} disabled={page >= count} onClick={() => onPage(page + 1)} />
    </div>
  );
}

/** Картинка источника вместо текста её страницы: сама картинка, под ней описание — обычным текстом, это данные. */
function ImageBlock({ documentId, title, image }: { documentId: string; title: string; image: NonNullable<DocumentText['image']> }) {
  const [failed, setFailed] = useState(false);
  return (
    <>
      {failed ? (
        <Notice kind="error" action={{ label: texts.common.retry, onClick: () => setFailed(false) }}>
          {t.imageFailed}
        </Notice>
      ) : (
        <img
          className={`p-doc-image ${styles.image}`}
          src={kbImageUrl(documentId, image.id)}
          alt={t.imageAlt(title)}
          onError={() => setFailed(true)}
        />
      )}
      <h2 className={styles.imageTitle}>{t.imageDescription}</h2>
      <p className={styles.text}>{image.description}</p>
      <p className={styles.note}>{t.imageNote}</p>
    </>
  );
}

/** Строки расшифровки: отрезок с моментом начинает новую строку, без момента — продолжает текущую. */
function transcriptLines(segments: Segment[]): { start: number | null; segments: Segment[] }[] {
  const lines: { start: number | null; segments: Segment[] }[] = [];
  for (const segment of segments) {
    const last = lines.at(-1);
    if (segment.start_seconds !== null || !last) {
      lines.push({ start: segment.start_seconds, segments: [segment] });
    } else {
      last.segments.push(segment);
    }
  }
  return lines;
}

interface TranscriptProps {
  text: DocumentText;
  firstMark: React.Ref<HTMLElement>;
  /** Слушать с момента; `null` — звук не загрузился: моменты остаются текстом, не кнопками. */
  onPlay: ((seconds: number) => void) | null;
  onPage: (page: number) => void;
}

/** Расшифровка открытой части записи (концепция §4.4): слева момент отрезка речи, справа его текст. */
function Transcript({ text, firstMark, onPlay, onPage }: TranscriptProps) {
  return (
    <>
      {text.page !== null && text.page_count !== null && text.page_count > 1 && (
        <Pager
          page={text.page}
          count={text.page_count}
          label={t.partOf(text.page, text.page_count)}
          previous={t.previousPart}
          next={t.nextPart}
          onPage={onPage}
        />
      )}
      {text.segments.length === 0 && <p className={styles.note}>{t.noSpeech}</p>}
      {transcriptLines(text.segments).map(({ start, segments }, index) => (
        <div key={index} className={styles.line}>
          {start !== null &&
            (onPlay ? (
              <button
                type="button"
                className={styles.moment}
                aria-label={t.listenFromSpoken(spokenTimecode(start))}
                onClick={() => onPlay(start)}
              >
                {formatTimecode(start)}
              </button>
            ) : (
              <span className={styles.momentText}>{formatTimecode(start)}</span>
            ))}
          <p className={styles.text}>
            <Segments segments={segments} firstMark={firstMark} />
          </p>
        </div>
      ))}
    </>
  );
}

interface DocumentViewerProps {
  opened: Opened;
  onPage: (page: number) => void;
  onClose: () => void;
}

function DocumentViewer({ opened, onPage, onClose }: DocumentViewerProps) {
  const { target, document, text, openId } = opened;
  const firstMark = useScrollToQuote(text);
  const audio = useRef<HTMLAudioElement>(null);
  // Элемент — в состоянии, а не в `ref`: содержимое панели библиотека монтирует позже самого компонента.
  const [playerArea, setPlayerArea] = useState<HTMLDivElement | null>(null);
  const title = document?.title ?? target.title;
  const page = text?.page ?? null;
  const pageCount = text?.page_count ?? null;
  // Запись отличает момент, с которого слушать: у документов его нет (контракт §8.3).
  const listenAt = text?.timecode_seconds ?? null;
  // Проигрыватель встаёт на момент, с которым запись открыта; при листании частей он не переставляется.
  // У нового источника проигрыватель и его состояние начинаются заново.
  const [player, setPlayer] = useState({ openId, startAt: listenAt ?? 0, failed: false });
  if (player.openId !== openId) {
    setPlayer({ openId, startAt: listenAt ?? 0, failed: false });
  }

  const headerHeight = useHeaderHeight(playerArea);

  const playFrom = (seconds: number) => {
    const element = audio.current;
    if (!element) {
      return;
    }
    element.currentTime = seconds;
    element.play().catch(() => {
      // Звук не запустился: о сбое загрузки сообщает сам проигрыватель — заметкой на своём месте.
    });
  };

  return (
    <SidePage width={WIDTH} blockBackground={false} ignoreOutsideClick onClose={onClose}>
      <SidePage.Header>
        <span className={styles.title}>{title}</span>
        {document && (
          <span className={styles.meta}>
            {t.scopes[document.scope]}
            {' · '}
            {document.author.is_me ? t.addedByMe : t.addedBy(shortName(document.author.full_name))}
            <br />
            {formatDate(document.created_at)}
            {document.page_count !== null && listenAt === null && ` · ${t.pages(document.page_count)}`}
            {document.duration_seconds !== null && ` · ${formatDuration(document.duration_seconds)}`}
            {document.images_truncated && (
              <>
                <br />
                {texts.kb.imagesTruncated}
              </>
            )}
          </span>
        )}
      </SidePage.Header>
      <SidePage.Body>
        <SidePage.Container>
          <div className={styles.body}>
          {opened.failedPage !== null && (
            <Notice
              kind="error"
              action={{ label: texts.common.retry, onClick: () => onPage(opened.failedPage as number) }}
            >
              {t.pageFailed}
            </Notice>
          )}
          {listenAt !== null && (
            <div ref={setPlayerArea} className={styles.player} style={{ top: headerHeight }}>
              {/* Момент — самое начало записи: хватает пуска в проигрывателе. */}
              {listenAt > 0 && !player.failed && (
                <Button icon={<IconMediaUiAPlayRegular16 />} onClick={() => playFrom(listenAt)}>
                  {t.listenFrom(formatTimecode(listenAt))}
                </Button>
              )}
              <AudioPlayer
                key={openId}
                ref={audio}
                src={kbAudioUrl(target.documentId)}
                label={t.audioLabel(title)}
                startAt={player.startAt}
                failed={player.failed}
                onFailedChange={(failed) => setPlayer({ ...player, failed })}
              />
              <p className={styles.note}>{t.transcribed}</p>
            </div>
          )}
          <Loader active={opened.loading} caption={texts.common.loading} delayBeforeSpinnerShow={300}>
            {text && listenAt !== null && (
              <Transcript text={text} firstMark={firstMark} onPlay={player.failed ? null : playFrom} onPage={onPage} />
            )}
            {text && listenAt === null && (
              <>
                {page !== null && pageCount !== null && (
                  <Pager
                    page={page}
                    count={pageCount}
                    label={t.pageOf(page, pageCount)}
                    previous={t.previous}
                    next={t.next}
                    onPage={onPage}
                  />
                )}
                {text.image ? (
                  <ImageBlock key={text.image.id} documentId={target.documentId} title={title} image={text.image} />
                ) : (
                  <>
                    {text.recognized && <p className={styles.recognized}>{t.recognized}</p>}
                    <p className={styles.text}>
                      <Segments segments={text.segments} firstMark={firstMark} />
                    </p>
                  </>
                )}
              </>
            )}
          </Loader>
          </div>
        </SidePage.Container>
      </SidePage.Body>
      <SidePage.Footer panel>
        {listenAt === null ? (
          <Link href={kbFileUrl(target.documentId, page)} target="_blank" rel="noopener noreferrer">
            {t.openOriginal}
          </Link>
        ) : (
          <span className={styles.note}>{t.noOriginal}</span>
        )}
      </SidePage.Footer>
    </SidePage>
  );
}
