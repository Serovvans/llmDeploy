import { IconAttachPaperclipRegular16 } from '@skbkontur/icons/IconAttachPaperclipRegular16';
import { IconWarningTriangleRegular16 } from '@skbkontur/icons/IconWarningTriangleRegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Button, Hint, Loader, ScrollContainer, Select } from '@skbkontur/react-ui';
import { useRef, useState } from 'react';

import type { DialogCarried, DialogKind, DialogSummary, MemoryMode, MemoryPair } from '../../api/types';
import type { DialogMemory } from '../../memory/useDialogMemory';
import { texts } from '../../texts';
import { Notice } from '../Notice';
import { ProgressBar } from '../ProgressBar';
import { isWarningLevel, levelHint, levelPercent } from './levelHint';
import { CarriedBlock, SummarizedPairs, SummaryBlock, summarizedRange } from './MemoryBlocks';
import styles from './MemoryPanel.module.css';

const t = texts.memory;
const SELECT_WIDTH_PX = 160;

/** Куда перевести фокус при открытии панели: заголовок или выбор вопроса (концепция §9). */
export interface PanelFocus {
  target: 'heading' | { questionId: string };
  /** Номер запроса: одинаковые запросы подряд тоже выполняются. */
  n: number;
}

function modeItems(pair: MemoryPair): [MemoryMode, string, string][] {
  const comments = t.modeComments;
  return [
    ['full', t.modes.full, pair.attachments > 0 ? comments.full : comments.fullWithoutAttachments],
    ...(pair.attachments > 0 ? [['text_only', t.modes.text_only, comments.text_only] as [MemoryMode, string, string]] : []),
    ['excluded', t.modes.excluded, comments.excluded],
  ];
}

interface PairRowProps {
  pair: MemoryPair;
  n: number;
  busy: boolean;
  error: string | undefined;
  onMode: (mode: MemoryMode) => void;
  onHover: (questionId: string | null) => void;
  onReveal: (questionId: string) => void;
  onSelectOpen: (open: boolean) => void;
}

/** Строка описи: номер и начало вопроса — кнопка «показать в ленте», под ними выбор и пометки. */
function PairRow({ pair, n, busy, error, onMode, onHover, onReveal, onSelectOpen }: PairRowProps) {
  const preview = useRef<HTMLSpanElement>(null);
  const [hintOpen, setHintOpen] = useState(false);
  const excluded = pair.memory_mode === 'excluded';
  const wontFit = pair.dropped && !excluded;
  const marks = (pair.attachments > 0 ? 1 : 0) + (wontFit ? 1 : 0);
  // Полное начало — только у обрезанного текста (концепция §6).
  const openHint = () => {
    const element = preview.current;
    setHintOpen(pair.question_preview !== '' && element !== null && element.scrollHeight > element.clientHeight);
  };
  const leave = () => {
    setHintOpen(false);
    onHover(null);
  };
  return (
    <li className={excluded ? `${styles.row} ${styles.excluded}` : styles.row}>
      <Hint text={pair.question_preview} pos="left" maxWidth={320} manual opened={hintOpen}>
        <button
          type="button"
          className={styles.show}
          aria-label={t.showInFeed(n, pair.question_preview || t.onlyAttachments)}
          onMouseEnter={() => {
            openHint();
            onHover(pair.question_id);
          }}
          onMouseLeave={leave}
          onFocus={(event) => {
            if (event.currentTarget.matches(':focus-visible')) {
              openHint();
            }
            onHover(pair.question_id);
          }}
          onBlur={leave}
          onClick={() => onReveal(pair.question_id)}
        >
          <span className={styles.number}>{n}</span>
          <span ref={preview} className={styles.preview}>
            {pair.question_preview || <i>{t.onlyAttachments}</i>}
          </span>
        </button>
      </Hint>
      <div className={styles.controls}>
        <span data-memory-select={pair.question_id}>
          <Select<MemoryMode, string>
            size="small"
            width={SELECT_WIDTH_PX}
            aria-label={t.modeLabel(n)}
            items={modeItems(pair)}
            value={pair.memory_mode}
            disabled={busy}
            onValueChange={onMode}
            onOpen={() => onSelectOpen(true)}
            onClose={() => onSelectOpen(false)}
          />
        </span>
        {marks > 0 && (
          // Обе пометки сразу переносятся на вторую строку (концепция §4.9).
          <span className={marks > 1 ? `${styles.marks} ${styles.marksBelow}` : styles.marks}>
            {pair.attachments > 0 && (
              <span className={styles.mark}>
                <IconAttachPaperclipRegular16 aria-hidden="true" />
                {t.attachments(pair.attachments)}
              </span>
            )}
            {wontFit && (
              <Hint text={t.wontFitHint} maxWidth={320}>
                <span className={styles.mark}>
                  <span className={styles.warningIcon} aria-hidden="true">
                    <IconWarningTriangleRegular16 />
                  </span>
                  {t.wontFit}
                  <span className="p-visually-hidden">{`. ${t.wontFitHint}`}</span>
                </span>
              </Hint>
            )}
          </span>
        )}
      </div>
      {error && (
        <p className={styles.error} role="alert">
          {error}
        </p>
      )}
    </li>
  );
}

interface MemoryPanelProps {
  id: string;
  kind: DialogKind;
  memory: DialogMemory;
  /** Формируется ответ — здесь или в другой вкладке: выбор недоступен. */
  busy: boolean;
  /** Крестик или Esc: фокус возвращает экран — туда, откуда панель открыли. */
  onClose: () => void;
  onHover: (questionId: string | null) => void;
  onReveal: (questionId: string) => void;
  /** Пересказ и перенесённое диалога (0.8): блоки над описью. */
  summary: DialogSummary | null;
  carried: DialogCarried | null;
  /** `memory.keep_recent_pairs` — число в пояснениях к сжатию. */
  keepRecent: number;
  onCompress: () => void;
  onEditSummary: () => void;
  onCancelSummary: () => void;
  /** «Перенести в новый диалог…» в подвале; `null` — диалог ещё не создан на сервере, подвала нет. */
  onTransfer: (() => void) | null;
}

/**
 * Панель памяти диалога (свой: `MemoryPanel`, концепция §4.9): уровень, опись вопросов с ответами и
 * выбор, как модель учитывает каждый. Колонка справа или слой поверх ленты — решают стили по ширине окна.
 */
export function MemoryPanel(props: MemoryPanelProps) {
  const { id, kind, memory, busy, onClose, onHover, onReveal } = props;
  const selectOpen = useRef(false);
  const { status, level } = memory;
  const pairs = memory.memory?.pairs ?? [];
  const range = summarizedRange(pairs);
  const summarized = range ? pairs.slice(range.from - 1, range.to) : [];

  const group = kind === 'docparse' ? 'docparse' : 'dialog';
  const allExcluded = pairs.length > 0 && pairs.every((pair) => pair.memory_mode === 'excluded');
  const hint = levelHint(memory);

  let content: React.ReactNode;
  if (status === 'loading' && level === null) {
    content = (
      <Loader active caption={texts.common.loading} delayBeforeSpinnerShow={300}>
        <div className={styles.loading} />
      </Loader>
    );
  } else if (status === 'failed' || level === null) {
    content = (
      <div className={styles.failed}>
        <Notice kind="error" action={{ label: texts.common.retry, onClick: memory.retry }}>
          {t.loadFailed}
        </Notice>
      </div>
    );
  } else {
    content = (
      <>
        <div className={styles.level}>
          <p className={styles.levelTitle}>
            {level === 'overflow' && (
              <span className={styles.warningIcon} aria-hidden="true">
                <IconWarningTriangleRegular16 />
              </span>
            )}
            {t.levelTitles[level]}
          </p>
          <Hint text={hint} maxWidth={320}>
            <ProgressBar percent={levelPercent(memory)} warning={isWarningLevel(memory)} className={styles.bar} />
          </Hint>
        </div>
        <div className={styles.scroll}>
          <ScrollContainer>
            {/* Закреплены только уровень и полоса; пояснения прокручиваются, чтобы опись сохраняла высоту (§4.9). */}
            <div className={styles.notes}>
              <p className={styles.explain}>{t.explain[group][level]}</p>
              <p className={styles.muted}>{t.estimateNote}</p>
              {kind === 'chat' && <p className={styles.muted}>{t.chatNote}</p>}
              {kind === 'docparse' && <p className={styles.muted}>{t.docparseNote}</p>}
              {kind === 'code' && <p className={styles.muted}>{texts.workshop.assistant.memoryNote}</p>}
            </div>
            {/* Блоки 0.8 отделены линиями от пояснения и от описи, как на схеме панели (§4.9). */}
            <div className={styles.blocks}>
              {props.carried && <CarriedBlock carried={props.carried} />}
              {memory.memory && (
                <SummaryBlock
                  summary={props.summary}
                  range={range}
                  compressPairs={memory.memory.compress_pairs}
                  keepRecent={props.keepRecent}
                  busy={busy}
                  onCompress={props.onCompress}
                  onEdit={props.onEditSummary}
                  onCancel={props.onCancelSummary}
                />
              )}
            </div>
            <section className={styles.pairs}>
              <h3 className={styles.pairsTitle}>{t.pairsTitle}</h3>
              {pairs.length === 0 ? (
                <p className={styles.muted}>{t.noPairs}</p>
              ) : (
                <>
                  <p className={styles.muted}>{t.pairsHint}</p>
                  {busy && <Notice kind="info">{t.busy}</Notice>}
                  {allExcluded && <p className={styles.muted}>{t.allExcluded}</p>}
                  <ol className={styles.list}>
                    {range && <SummarizedPairs pairs={summarized} range={range} onReveal={onReveal} onHover={onHover} />}
                    {pairs.map((pair, index) => pair.summarized ? null : (
                      <PairRow
                        key={pair.question_id}
                        pair={pair}
                        n={index + 1}
                        busy={busy}
                        error={memory.rowErrors[pair.question_id]}
                        onMode={(mode) => memory.setMode(pair, mode)}
                        onHover={onHover}
                        onReveal={onReveal}
                        onSelectOpen={(open) => {
                          selectOpen.current = open;
                        }}
                      />
                    ))}
                  </ol>
                </>
              )}
            </section>
          </ScrollContainer>
        </div>
      </>
    );
  }

  return (
    <aside
      id={id}
      aria-label={t.title}
      data-memory-panel=""
      className={styles.panel}
      // Esc закрывает панель, когда список выбора закрыт: открытый список Esc закрывает сам (концепция §9).
      onKeyDownCapture={(event) => {
        if (event.key === 'Escape' && !selectOpen.current) {
          onClose();
        }
      }}
    >
      <div className={styles.head}>
        {/* Сюда переходит фокус при открытии панели (концепция §9). */}
        <h2 tabIndex={-1} className={styles.heading}>
          {t.title}
        </h2>
        <Hint text={t.hidePanel}>
          <Button use="text" icon={<IconXRegular16 />} aria-label={t.hidePanel} onClick={onClose} />
        </Hint>
      </div>
      {content}
      {props.onTransfer && (
        <div className={styles.footer}>
          <Button width="100%" onClick={props.onTransfer}>
            {t.transfer.menu}
          </Button>
        </div>
      )}
      <p className="p-visually-hidden" role="status">
        {memory.announcement}
      </p>
    </aside>
  );
}
