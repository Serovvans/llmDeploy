import { Button, Gapped, Modal, Radio, RadioGroup, Select } from '@skbkontur/react-ui';
import { useCallback, useEffect, useId, useRef, useState } from 'react';

import { api, NetworkError } from '../../api/client';
import type { Dialog, DialogKind, MemoryPair, Message } from '../../api/types';
import { draftNotice, type DraftExit } from '../../memory/draft';
import { useSummaryDraft } from '../../memory/useSummaryDraft';
import { texts } from '../../texts';
import { Notice } from '../Notice';
import { exitOf, saveFailure, type SaveFailure } from './CompressModal';
import styles from './MemoryModals.module.css';
import { SummaryDraft } from './SummaryDraft';

const t = texts.memory;
const tt = t.transfer;
type Target = Exclude<DialogKind, 'docparse'>;
const TARGETS: Target[] = ['chat', 'sql', 'cogis'];
/** Быстрый ответ не мигает индикатором загрузки (концепция §4.9). */
const LOADING_DELAY_MS = 300;
/** Начало вопроса в пункте выбора ответа — короче строки описи: пункт списка в одну строку. */
const ANSWER_PREVIEW_CHARS = 80;

type What = 'summary' | 'verbatim';
type AnswerStatus = 'idle' | 'loading' | 'failed' | 'network' | 'gone';

function answerLabel(pair: MemoryPair): string {
  const preview = pair.question_preview || t.onlyAttachments;
  return tt.answerItem(preview.length > ANSWER_PREVIEW_CHARS ? `${preview.slice(0, ANSWER_PREVIEW_CHARS)}…` : preview);
}

/** Ответы для «Один ответ дословно» — из описи: пары с текстом ответа, новые сверху (концепция §4.9, API-31). */
function answersOf(pairs: MemoryPair[] | null): MemoryPair[] {
  return (pairs ?? []).filter((pair) => pair.answer_preview !== '').reverse();
}

export interface TransferSource {
  id: string;
  kind: DialogKind;
  title: string | null;
}

interface TransferModalProps {
  source: TransferSource;
  /** «Продолжить отсюда»: ответ, до которого включительно пересказывается разговор; `null` — весь диалог. */
  until: string | null;
  /** Строка запроса оценки: по ней окно берёт опись (уровень оно не показывает). */
  query: Record<string, string>;
  /** Сообщения, уже загруженные в ленту: текст выбранного ответа отсюда берётся без запроса. */
  loaded: Message[];
  keepRecent: number;
  maxChars: number;
  onDone: (dialog: Dialog) => void;
  onExit: (exit: DraftExit) => void;
  onClose: () => void;
}

/** Окно «Перенести в новый диалог» и «Продолжить в новом диалоге» (концепция §4.9). */
export function TransferModal(props: TransferModalProps) {
  const { source, until, query, loaded, maxChars, onExit } = props;
  const continuing = until !== null;
  const draft = useSummaryDraft(source.id, onExit);
  const { state } = draft;
  const [target, setTarget] = useState<Target>(source.kind === 'docparse' ? 'chat' : source.kind);
  const [what, setWhat] = useState<What>('summary');
  const [pairs, setPairs] = useState<MemoryPair[] | null>(null);
  const [answerId, setAnswerId] = useState<string | null>(null);
  /** Чтение текста ответа; `seq` — номер чтения: ответ другого выбора в поле не встаёт. */
  const [reading, setReading] = useState<{ status: AnswerStatus; seq: number }>({ status: 'idle', seq: 0 });
  const [shownFor, setShownFor] = useState<number | null>(null);
  const answer = reading.status;
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<SaveFailure | null>(null);
  const readSeq = useRef(0);
  const container = useRef<HTMLDivElement>(null);
  const whereId = useId();
  const whatId = useId();
  const exit = useRef(onExit);
  useEffect(() => {
    exit.current = onExit;
  });

  const queryKey = JSON.stringify(query);
  const loadPairs = useCallback(() => {
    api.getMemory(source.id, JSON.parse(queryKey) as Record<string, string>).then(
      (memory) => setPairs(memory.pairs),
      (error: unknown) => {
        const gone = exitOf(error);
        if (gone) {
          exit.current(gone);
        }
        // Описи нет — нет и варианта «Один ответ дословно»; пересказ от неё не зависит.
      },
    );
  }, [source.id, queryKey]);

  // При открытии фокус — на первом выборе («Куда», §9).
  useEffect(() => {
    if (!continuing) {
      loadPairs();
    }
    container.current?.querySelector<HTMLElement>('input[type="radio"]:checked')?.focus();
  }, [continuing, loadPairs]);

  useEffect(() => {
    if (reading.status !== 'loading') {
      return;
    }
    const timer = window.setTimeout(() => setShownFor(reading.seq), LOADING_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [reading]);
  const loadingShown = answer === 'loading' && shownFor === reading.seq;

  /** Текст ответа: из ленты сразу, иначе — страницами `GET …/messages` до него (контракт §5.10.7). */
  const readAnswer = async (id: string) => {
    const seq = (readSeq.current += 1);
    const setAnswer = (status: AnswerStatus) => setReading({ status, seq });
    setFailure(null);
    const inFeed = loaded.find((message) => message.id === id);
    if (inFeed) {
      setAnswer('idle');
      draft.load(inFeed.content, null);
      return;
    }
    draft.load('', null);
    setAnswer('loading');
    let cursor: string | null = null;
    try {
      do {
        const page = await api.listMessages(source.id, cursor);
        if (seq !== readSeq.current) {
          // Выбран другой ответ: в поле встаёт последний выбранный.
          return;
        }
        const found = page.items.find((message) => message.id === id);
        if (found) {
          setAnswer('idle');
          draft.load(found.content, null);
          return;
        }
        cursor = page.next_cursor;
      } while (cursor);
      // Ответ сформировали заново в другой вкладке: опись запрашивается заново.
      setAnswer('gone');
      setAnswerId(null);
      loadPairs();
    } catch (error) {
      if (seq !== readSeq.current) {
        return;
      }
      const gone = exitOf(error);
      if (gone) {
        onExit(gone);
        return;
      }
      setAnswer(error instanceof NetworkError ? 'network' : 'failed');
    }
  };

  const chooseWhat = (next: What) => {
    setWhat(next);
    setFailure(null);
    if (next === 'verbatim') {
      const id = answerId ?? answersOf(pairs)[0]?.answer_id ?? null;
      setAnswerId(id);
      if (id) {
        void readAnswer(id);
      } else {
        draft.load('', null);
      }
    } else {
      readSeq.current += 1;
      setReading({ status: 'idle', seq: readSeq.current });
      draft.reset();
    }
  };

  const prepare = () => {
    setFailure(null);
    draft.start(continuing ? { purpose: 'transfer', until_message_id: until } : { purpose: 'transfer' });
  };

  const submit = async () => {
    const text = state.text.trim();
    let problem: string | null = null;
    if (what === 'summary' && state.phase === 'idle') {
      problem = tt.prepareFirst;
    } else if (state.phase === 'streaming') {
      problem = t.draft.wait;
    } else if (answer === 'loading') {
      problem = tt.waitAnswer;
    } else if (!text) {
      problem = tt.enter;
    } else if (state.text.length > maxChars) {
      problem = tt.tooLong(maxChars);
    }
    if (problem) {
      setFailure({ notice: null, field: problem });
      return;
    }
    setSaving(true);
    setFailure(null);
    try {
      props.onDone(await api.createDialog(target, { text: state.text, source_dialog_id: source.id }));
    } catch (error) {
      setSaving(false);
      const gone = exitOf(error);
      if (gone) {
        onExit(gone);
        return;
      }
      setFailure(
        saveFailure(error, 'carried.text', {
          enter: tt.enter,
          tooLong: tt.tooLong(maxChars),
          changed: { text: texts.common.actionFailed, action: null },
        }),
      );
    }
  };

  const answers = answersOf(pairs);
  const verbatimOffered = !continuing && answers.length > 0;
  const showField = what === 'verbatim' || state.phase !== 'idle';
  const title = source.title ?? texts.dialogs[source.kind].newLabel;
  const intro = [continuing ? tt.continueIntro : null, source.kind === 'docparse' && what === 'summary' ? tt.docparseIntro : null]
    .filter(Boolean)
    .join('. ');
  // «Дождитесь…» снимается, когда пересказ закончился: подсказка про то, чего уже нет, вводит в заблуждение.
  const fieldError = failure?.field === t.draft.wait && state.phase !== 'streaming' ? null : (failure?.field ?? null);
  const notice =
    failure?.notice ??
    (state.failure
      ? draftNotice(state.failure, state.text.length > 0, { purpose: 'transfer', keepRecent: props.keepRecent, verbatim: verbatimOffered })
      : null);
  const answerNotice =
    answer === 'failed' || answer === 'network' ? (
      <Notice kind="error" action={{ label: texts.common.retry, onClick: () => answerId && void readAnswer(answerId) }}>
        {answer === 'network' ? texts.common.noConnection : tt.answerFailed}
      </Notice>
    ) : answer === 'gone' ? (
      <Notice kind="error">{tt.answerGone}</Notice>
    ) : null;

  return (
    <Modal width={720} ignoreBackgroundClick disableClose={saving} onClose={props.onClose}>
      <Modal.Header>{continuing ? tt.continueTitle : tt.title}</Modal.Header>
      <Modal.Body>
        <div ref={container} className={styles.body}>
          <p className={styles.muted}>{tt.from(title)}</p>
          <div className={styles.row}>
            <span id={whereId} className={styles.label}>
              {tt.where}
            </span>
            <div role="group" aria-labelledby={whereId}>
              <RadioGroup<Target> value={target} onValueChange={setTarget} disabled={saving}>
                <Gapped gap={24}>
                  {TARGETS.map((kind) => (
                    <Radio<Target> key={kind} value={kind}>
                      {tt.kinds[kind]}
                    </Radio>
                  ))}
                </Gapped>
              </RadioGroup>
            </div>
          </div>
          {intro && <p className={styles.intro}>{intro}</p>}
          {!continuing && (
            <div className={styles.row}>
              <span id={whatId} className={styles.label}>
                {tt.what}
              </span>
              <div role="group" aria-labelledby={whatId}>
                <RadioGroup<What> width="100%" value={what} onValueChange={chooseWhat} disabled={saving}>
                  <div className={styles.options}>
                    <div className={styles.option}>
                      <Radio<What> value="summary">{tt.summary}</Radio>
                      <span className={styles.optionNote}>{tt.summaryNote}</span>
                    </div>
                    {verbatimOffered && (
                      <div className={styles.option}>
                        <Radio<What> value="verbatim">{tt.verbatim}</Radio>
                        <span className={styles.optionNote}>{tt.verbatimNote}</span>
                        {what === 'verbatim' && (
                          <div className={styles.answer}>
                            <Select<string, string>
                              width="100%"
                              aria-label={tt.verbatim}
                              items={answers.map((pair) => [pair.answer_id, answerLabel(pair)] as [string, string])}
                              value={answerId ?? undefined}
                              disabled={saving}
                              onValueChange={(id) => {
                                setAnswerId(id);
                                void readAnswer(id);
                              }}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </RadioGroup>
              </div>
            </div>
          )}
          {showField ? (
            <SummaryDraft
              state={state}
              label={what === 'verbatim' ? tt.verbatim : tt.summary}
              maxChars={maxChars}
              notice={notice}
              fieldError={fieldError}
              disclaimer={what === 'summary' ? tt.disclaimer : null}
              loading={answer === 'loading' ? (loadingShown ? tt.loadingAnswer : '') : null}
              extra={answerNotice}
              onEdit={(text) => {
                draft.edit(text);
                setFailure(null);
              }}
              onStop={draft.stop}
              onRestart={prepare}
            />
          ) : (
            <div className={styles.option}>
              <Button className={styles.prepare} onClick={prepare}>
                {tt.prepare}
              </Button>
              {failure?.field && (
                <p className={styles.error} role="alert">
                  {failure.field}
                </p>
              )}
            </div>
          )}
          <p className={styles.muted}>{tt.notTransferred}</p>
        </div>
      </Modal.Body>
      <Modal.Footer>
        <Gapped gap={8}>
          <Button use="primary" loading={saving} onClick={() => void submit()}>
            {tt.submit}
          </Button>
          <Button disabled={saving} onClick={props.onClose}>
            {texts.common.cancel}
          </Button>
        </Gapped>
      </Modal.Footer>
    </Modal>
  );
}
