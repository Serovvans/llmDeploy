import { IconArrowCDownRegular16 } from '@skbkontur/icons/IconArrowCDownRegular16';
import { IconArrowCRightRegular16 } from '@skbkontur/icons/IconArrowCRightRegular16';
import { Button, Link } from '@skbkontur/react-ui';
import { useId, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import type { DialogCarried, DialogKind, DialogSummary, MemoryPair } from '../../api/types';
import { texts } from '../../texts';
import { MarkdownView } from '../chat/MarkdownView';
import { Notice } from '../Notice';
import { TitleHint } from '../TitleHint';
import styles from './MemoryBlocks.module.css';

const t = texts.memory;

const SECTION_PATHS: Record<DialogKind, string> = { chat: '/chat', sql: '/sql', cogis: '/cogis', docparse: '/documents' };

export function dialogPath(kind: DialogKind, id: string): string {
  return `${SECTION_PATHS[kind]}/${id}`;
}

/** Номера первой и последней пересказанной пары (концепция §4.9); `null` — пересказанных нет. */
export function summarizedRange(pairs: MemoryPair[]): { from: number; to: number } | null {
  // Пересказанные пары идут подряд с начала диалога: граница пересказа одна.
  const count = pairs.filter((pair) => pair.summarized).length;
  const first = pairs.findIndex((pair) => pair.summarized);
  return first < 0 ? null : { from: first + 1, to: first + count };
}

/** Текст пересказа или сведений под «Показать»: Markdown с санацией, на `--p-surface-2`, не выше 320 px. */
function Expanded({ id, text }: { id: string; text: string }) {
  return (
    <div id={id} className={styles.expanded}>
      <MarkdownView text={text} />
    </div>
  );
}

/** «Показать» / «Скрыть» с `aria-expanded` (§9). */
function ToggleLink(props: { open: boolean; controls: string; show: string; hide: string; onToggle: () => void }) {
  return (
    <Link component="button" aria-expanded={props.open} aria-controls={props.controls} onClick={props.onToggle}>
      {props.open ? props.hide : props.show}
    </Link>
  );
}

/** Название исходного диалога: ссылка на него; удалён — обычный текст (ПД §5.7, API-30). */
function SourceTitle({ carried }: { carried: DialogCarried }) {
  const navigate = useNavigate();
  const title = carried.source_title ?? texts.dialogs[carried.source_kind ?? 'chat'].newLabel;
  if (!carried.source_dialog_id || !carried.source_kind) {
    return <>«{title}»</>;
  }
  const href = dialogPath(carried.source_kind, carried.source_dialog_id);
  return (
    <>
      «
      <Link
        href={href}
        onClick={(event) => {
          event.preventDefault();
          navigate(href);
        }}
      >
        {title}
      </Link>
      »
    </>
  );
}

/** «Сведения из „…“»; источник удалён и без названия — «Сведения из удалённого диалога» (ПД §5.7). */
function CarriedHeading({ carried }: { carried: DialogCarried }) {
  if (carried.source_kind === null && carried.source_title === null) {
    return <>{t.carried.deleted}</>;
  }
  return (
    <>
      {t.carried.title}
      <SourceTitle carried={carried} />
    </>
  );
}

/** Блок «Сведения из „…“» в панели памяти (0.8): выбора и сжатия у сведений нет. */
export function CarriedBlock({ carried }: { carried: DialogCarried }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <section className={styles.block}>
      <h3 className={styles.title}>
        <CarriedHeading carried={carried} />
      </h3>
      <p className={styles.muted}>{t.carried.note}</p>
      <p>
        <ToggleLink open={open} controls={id} show={t.carried.show} hide={t.carried.hide} onToggle={() => setOpen(!open)} />
      </p>
      {open && <Expanded id={id} text={carried.text} />}
    </section>
  );
}

/** Заметка над лентой диалога, созданного переносом (0.8): что модель знает с самого начала. */
export function CarriedNotice({ carried }: { carried: DialogCarried }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className={styles.notice}>
      <Notice
        kind="info"
        action={{ label: open ? t.carried.hide : t.carried.show, expanded: open, controls: id, onClick: () => setOpen(!open) }}
      >
        <CarriedHeading carried={carried} />
        {t.carried.notice}
      </Notice>
      {open && <Expanded id={id} text={carried.text} />}
    </div>
  );
}

interface CompressButtonProps {
  compressPairs: { from: number; to: number } | null;
  /** Есть применённый пересказ: пояснение — о повторном сжатии. */
  repeat: boolean;
  keepRecent: number;
  busy: boolean;
  onCompress: () => void;
}

/** «Сжать раннюю часть» с пояснением; сжимать нечего — кнопка в обходе по Tab, причина — в подсказке (§9). */
function CompressButton({ compressPairs, repeat, keepRecent, busy, onCompress }: CompressButtonProps) {
  const noteId = useId();
  const reasonId = useId();
  if (!compressPairs) {
    const reason = t.summary.nothing(keepRecent);
    return (
      <div className={styles.compress}>
        {/* Like the unavailable «Ответить заново»: the reason opens on hover and keyboard focus. */}
        <TitleHint text={reason} maxWidth={320} always className={styles.unavailableFocus}>
          <Button aria-disabled="true" aria-describedby={reasonId} className={styles.unavailable}>
            {t.summary.compress}
          </Button>
        </TitleHint>
        <span id={reasonId} className="p-visually-hidden">
          {reason}
        </span>
        <p className={styles.muted}>
          {t.summary.compressNote(keepRecent)}
        </p>
      </div>
    );
  }
  return (
    <div className={styles.compress}>
      {/* Во время ответа недоступна: заметка над описью объясняет причину (§4.9). */}
      <Button disabled={busy} aria-describedby={noteId} onClick={onCompress}>
        {t.summary.compress}
      </Button>
      <p id={noteId} className={styles.muted}>
        {repeat ? t.summary.recompressNote(compressPairs.from, compressPairs.to) : t.summary.compressNote(keepRecent)}
      </p>
    </div>
  );
}

interface SummaryBlockProps extends Omit<CompressButtonProps, 'repeat'> {
  summary: DialogSummary | null;
  range: { from: number; to: number } | null;
  onEdit: () => void;
  onCancel: () => void;
}

/** Блок «Пересказ ранней части» в панели памяти (0.8, концепция §4.9). */
export function SummaryBlock({ summary, range, onEdit, onCancel, ...compress }: SummaryBlockProps) {
  const [open, setOpen] = useState(false);
  const id = useId();
  if (!summary) {
    return (
      <section className={styles.block}>
        <CompressButton {...compress} repeat={false} />
      </section>
    );
  }
  return (
    <section className={styles.block}>
      <h3 className={styles.title}>{t.summary.title}</h3>
      {range && <p className={styles.muted}>{t.summary.range(range.from, range.to)}</p>}
      <p className={styles.links}>
        <ToggleLink open={open} controls={id} show={t.summary.show} hide={t.summary.hide} onToggle={() => setOpen(!open)} />
        <span aria-hidden="true">{' · '}</span>
        <Link component="button" onClick={onEdit}>
          {t.summary.edit}
        </Link>
        <span aria-hidden="true">{' · '}</span>
        <Link component="button" onClick={onCancel}>
          {t.summary.cancel}
        </Link>
      </p>
      {open && <Expanded id={id} text={summary.text} />}
      {compress.compressPairs && <CompressButton {...compress} repeat />}
    </section>
  );
}

interface SummarizedPairsProps {
  pairs: MemoryPair[];
  range: { from: number; to: number };
  onReveal: (questionId: string) => void;
  onHover: (questionId: string | null) => void;
}

/** Пересказанные вопросы — одной строкой-кнопкой в начале описи; выбора у них нет (0.8, §4.9). */
export function SummarizedPairs({ pairs, range, onReveal, onHover }: SummarizedPairsProps) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const Arrow = open ? IconArrowCDownRegular16 : IconArrowCRightRegular16;
  return (
    <li className={styles.summarized}>
      <button type="button" className={styles.toggle} aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        <Arrow aria-hidden="true" />
        <span>{t.summary.collapsed(range.from, range.to)}</span>
      </button>
      {open && (
        <ol id={id} className={styles.summarizedList}>
          {pairs.map((pair, index) => {
            const n = range.from + index;
            const preview = pair.question_preview || t.onlyAttachments;
            return (
              <li key={pair.question_id} className={styles.summarizedRow}>
                <button
                  type="button"
                  className={styles.show}
                  aria-label={t.showInFeed(n, preview)}
                  onMouseEnter={() => onHover(pair.question_id)}
                  onMouseLeave={() => onHover(null)}
                  onFocus={() => onHover(pair.question_id)}
                  onBlur={() => onHover(null)}
                  onClick={() => onReveal(pair.question_id)}
                >
                  <span className={styles.number}>{n}</span>
                  <span className={styles.preview}>{pair.question_preview || <i>{t.onlyAttachments}</i>}</span>
                </button>
                <span className={styles.mark}>
                  {pair.memory_mode === 'excluded' ? t.summary.notCounted : t.summary.inSummary}
                </span>
              </li>
            );
          })}
        </ol>
      )}
    </li>
  );
}

/**
 * Граница пересказа в ленте (свой: `SummaryBoundary`, 0.8): линии по краям, в середине — сколько сообщений
 * модель помнит кратко, и раскрытие пересказа. Видна и при выключенной функции (Д-36).
 */
export function SummaryBoundary({ summary }: { summary: DialogSummary }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <div className={styles.boundary}>
      <p className={styles.boundaryLine}>
        <span className={styles.boundaryText}>
          {t.summary.boundary(summary.messages)}
          {' · '}
          <ToggleLink
            open={open}
            controls={id}
            show={t.summary.showInFeed}
            hide={t.summary.hideInFeed}
            onToggle={() => setOpen(!open)}
          />
        </span>
      </p>
      {open && (
        <section id={id} className={styles.boundarySummary}>
          <h3 className={styles.title}>{t.summary.title}</h3>
          <MarkdownView text={summary.text} />
        </section>
      )}
    </div>
  );
}
