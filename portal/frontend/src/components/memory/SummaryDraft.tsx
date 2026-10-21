import { Button, Link, Spinner, Textarea } from '@skbkontur/react-ui';
import { useId } from 'react';

import { STREAM_CARET, type DraftNotice, type DraftState } from '../../memory/draft';
import { texts } from '../../texts';
import { Notice } from '../Notice';
import styles from './SummaryDraft.module.css';

const t = texts.memory.draft;
const MAX_ROWS = 16;

interface SummaryDraftProps {
  state: DraftState;
  /** Доступное имя поля. */
  label: string;
  maxChars: number;
  /** Отказ черновика — заметка над полем. */
  notice: DraftNotice | null;
  /** Ошибка поля или отказ кнопки окна — под полем. */
  fieldError: string | null;
  /** Оговорка под полем: у текста от модели — всегда; у ответа дословно — нет, и фазы пересказа не объявляются. */
  disclaimer: string | null;
  /**
   * Поле ждёт текст не от модели — ответ дословно грузится: поле только для чтения; непустая строка —
   * подпись у индикатора («Загрузка ответа…» показывается не сразу). `null` — не ждёт.
   */
  loading?: string | null;
  /** Заметка над полем от окна (сбой загрузки ответа). */
  extra?: React.ReactNode;
  textareaRef?: React.Ref<Textarea>;
  onEdit: (text: string) => void;
  onStop: () => void;
  /** «Подготовить заново» и «Повторить» — новый черновик с той же целью. */
  onRestart: () => void;
}

/**
 * Черновик пересказа (свой: `SummaryDraft`, концепция §4.9) — одинаковый в окнах сжатия и переноса:
 * поток с чертой, «Остановить», правка, счётчик символов, оговорка. Фазы объявляются, текст — нет (§9).
 */
export function SummaryDraft({ textareaRef, ...props }: SummaryDraftProps) {
  const { state, maxChars, notice } = props;
  const errorId = useId();
  const streaming = state.phase === 'streaming';
  const over = state.text.length > maxChars;
  const phases: Partial<Record<DraftState['phase'], string>> = props.disclaimer ? t.announce : {};
  const announce = props.loading || (phases[state.phase] ?? '');
  // Поле правится, когда текста больше не ждут: пока идёт поток или грузится ответ, оно только для чтения.
  const readOnly =
    streaming || (props.loading ?? null) !== null || (state.phase === 'failed' && state.until === undefined && !state.text);

  return (
    <div className={styles.draft}>
      {state.dropped > 0 && (
        <div role="alert">
          <Notice kind="warning">{t.dropped}</Notice>
        </div>
      )}
      {notice && (
        <Notice
          kind="error"
          action={notice.action ? { label: notice.action === 'restart' ? t.restart : texts.common.retry, onClick: props.onRestart } : undefined}
        >
          {notice.text}
        </Notice>
      )}
      {props.extra}
      <Textarea
        ref={textareaRef}
        width="100%"
        rows={6}
        autoResize
        maxRows={MAX_ROWS}
        aria-label={props.label}
        aria-describedby={props.fieldError ? errorId : undefined}
        error={over || Boolean(props.fieldError)}
        readOnly={readOnly}
        value={streaming ? state.text + STREAM_CARET : state.text}
        onValueChange={(value) => {
          if (!readOnly) {
            props.onEdit(value);
          }
        }}
      />
      <div className={styles.status}>
        <span className={styles.state}>
          {streaming && (
            <>
              <Spinner type="mini" caption={null} />
              <span>{t.streaming}</span>
              <Button size="small" onClick={props.onStop}>
                {t.stop}
              </Button>
            </>
          )}
          {props.loading && (
            <>
              <Spinner type="mini" caption={null} />
              <span>{props.loading}</span>
            </>
          )}
        </span>
        <span className={over ? `${styles.counter} ${styles.over}` : styles.counter}>{t.counter(state.text.length, maxChars)}</span>
      </div>
      {state.phase === 'stopped' && (
        <p className={styles.muted}>
          {t.stopped}{' '}
          <Link component="button" onClick={props.onRestart}>
            {t.restart}
          </Link>
        </p>
      )}
      {state.phase === 'ready' && state.truncated && <p className={styles.muted}>{t.truncated}</p>}
      {props.fieldError && (
        <p id={errorId} className={styles.error} role="alert">
          {props.fieldError}
        </p>
      )}
      {props.disclaimer && <p className={styles.muted}>{props.disclaimer}</p>}
      {/* Объявляются фазы и загрузка ответа, а не текст по словам (§9). */}
      <p className="p-visually-hidden" aria-live="polite">
        {announce}
      </p>
    </div>
  );
}
