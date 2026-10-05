import { IconArrowCLeftRegular16 } from '@skbkontur/icons/IconArrowCLeftRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { Button, Link, Loader, SidePage, SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { api, isApiError, kbFileUrl } from '../../api/client';
import type { DocumentText, KbDocument } from '../../api/types';
import { useFocusReturn } from '../../hooks/useFocusReturn';
import { formatDate, shortName, texts } from '../../texts';
import { Notice } from '../Notice';
import styles from './DocumentViewer.module.css';

const t = texts.viewer;
const WIDTH = 560;

export interface ViewerTarget {
  documentId: string;
  /** Название до ответа сервера: из карточки источника или строки списка. */
  title: string;
  /** Цитата: сервер сам выберет её страницу и подсветит отрезки. */
  fragmentId?: string;
  /** Документ из списка базы знаний; из карточки источника сведения запрашиваются отдельно. */
  document?: KbDocument;
}

interface Opened {
  target: ViewerTarget;
  /** Сведения для шапки; `null` — не получены: шапка остаётся с одним названием. */
  document: KbDocument | null;
  text: DocumentText | null;
  loading: boolean;
  /** Страница, которую не удалось открыть при листании. */
  failedPage: number | null;
}

/**
 * Просмотр документа рядом с ответом (концепция §4.4). Панель открывается после первого ответа
 * сервера: удалённый или необработанный документ её не открывает, а открытую — закрывает.
 */
export function useDocumentViewer() {
  const [opened, setOpened] = useState<Opened | null>(null);
  const [opening, setOpening] = useState<ViewerTarget | null>(null);
  const request = useRef(0);
  const rememberOpener = useFocusReturn(opened !== null);

  /** `current` — уже открытая панель (листание); `null` — первое открытие. */
  const load = useCallback(async (target: ViewerTarget, page: number | undefined, current: Opened | null) => {
    const id = (request.current += 1);
    if (current) {
      setOpened({ ...current, loading: true, failedPage: null });
    } else {
      setOpening(target);
    }
    // Цитата передаётся и при листании: сервер подсветит её, когда она на запрошенной странице (контракт §8.3).
    const where = { page, fragmentId: target.fragmentId };
    const known = current?.document ?? target.document ?? null;
    const meta = known ? Promise.resolve(known) : api.getKbDocument(target.documentId).catch(() => null);
    try {
      const [text, document] = await Promise.all([api.getKbDocumentText(target.documentId, where), meta]);
      if (id === request.current) {
        setOpened({ target, document, text, loading: false, failedPage: null });
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
      void load(target, undefined, null);
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

interface DocumentViewerProps {
  opened: Opened;
  onPage: (page: number) => void;
  onClose: () => void;
}

function DocumentViewer({ opened, onPage, onClose }: DocumentViewerProps) {
  const { target, document, text } = opened;
  const firstMark = useRef<HTMLElement>(null);
  const page = text?.page ?? null;
  const pageCount = text?.page_count ?? null;

  // Первый подсвеченный отрезок прокручивается в видимую часть.
  useEffect(() => {
    firstMark.current?.scrollIntoView({ block: 'center' });
  }, [text]);

  const firstHighlight = text?.segments.findIndex((segment) => segment.highlight) ?? -1;

  return (
    <SidePage width={WIDTH} blockBackground={false} ignoreOutsideClick onClose={onClose}>
      <SidePage.Header>
        <span className={styles.title}>{document?.title ?? target.title}</span>
        {document && (
          <span className={styles.meta}>
            {document.scope === 'shared' ? t.shared : t.personal}
            {' · '}
            {document.author.is_me ? t.addedByMe : t.addedBy(shortName(document.author.full_name))}
            <br />
            {formatDate(document.created_at)}
            {document.page_count !== null && ` · ${t.pages(document.page_count)}`}
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
          <Loader active={opened.loading} caption={texts.common.loading} delayBeforeSpinnerShow={300}>
            {text && (
              <>
                {page !== null && pageCount !== null && (
                  <div className={styles.pages}>
                    <Button
                      icon={<IconArrowCLeftRegular16 />}
                      aria-label={t.previous}
                      disabled={page <= 1}
                      onClick={() => onPage(page - 1)}
                    />
                    <span>{t.pageOf(page, pageCount)}</span>
                    <Button
                      icon={<IconArrowCRightRegular16 />}
                      aria-label={t.next}
                      disabled={page >= pageCount}
                      onClick={() => onPage(page + 1)}
                    />
                  </div>
                )}
                {text.recognized && <p className={styles.recognized}>{t.recognized}</p>}
                <p className={styles.text}>
                  {text.segments.map((segment, index) =>
                    segment.highlight ? (
                      <mark key={index} ref={index === firstHighlight ? firstMark : undefined} className={styles.mark}>
                        {segment.text}
                      </mark>
                    ) : (
                      segment.text
                    ),
                  )}
                </p>
              </>
            )}
          </Loader>
          </div>
        </SidePage.Container>
      </SidePage.Body>
      <SidePage.Footer panel>
        <Link href={kbFileUrl(target.documentId, page)} target="_blank" rel="noopener noreferrer">
          {t.openOriginal}
        </Link>
      </SidePage.Footer>
    </SidePage>
  );
}
