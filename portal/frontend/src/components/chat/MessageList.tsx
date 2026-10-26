import { IconArrowADownRegular16 } from '@skbkontur/icons/IconArrowADownRegular16';
import { IconArrowDCornerDownRightRegular16 } from '@skbkontur/icons/IconArrowDCornerDownRightRegular16';
import { IconArrowRoundSyncForwardRegular16 } from '@skbkontur/icons/IconArrowRoundSyncForwardRegular16';
import { IconCopyRegular16 } from '@skbkontur/icons/IconCopyRegular16';
import { IconEyeOffRegular16 } from '@skbkontur/icons/IconEyeOffRegular16';
import { IconNetDownloadRegular16 } from '@skbkontur/icons/IconNetDownloadRegular16';
import { Button, Link, Loader, Modal, ScrollContainer, Spinner } from '@skbkontur/react-ui';
import { Fragment, useEffect, useId, useMemo, useRef, useState } from 'react';

import { attachmentFileUrl } from '../../api/client';
import type { Attachment, Source } from '../../api/types';
import { exportDocx } from '../../chat/exportDocx';
import { answerErrorText } from '../../chat/files';
import type { ChatMessage, DialogView, Generation } from '../../chat/state';
import { useCopy } from '../../hooks/useCopy';
import { useFocusReturn } from '../../hooks/useFocusReturn';
import type { FeedMemory } from '../../memory/useMemoryUi';
import { texts } from '../../texts';
import { SummaryBoundary } from '../memory/MemoryBlocks';
import { Notice } from '../Notice';
import { TitleHint } from '../TitleHint';
import { AttachmentChip, isImage } from './AttachmentChip';
import { MarkdownView, type SourceLinks } from './MarkdownView';
import styles from './MessageList.module.css';
import { SourceCard } from './SourceCard';
import { SourceFigure } from './SourceFigure';
import { sqlRunAnnouncement } from './SqlRunResult';
import { ThinkingBlock } from './ThinkingBlock';

const t = texts.chat;
const BUSY_AFTER_MS = 20_000;
/** Ближе этого к нижнему краю лента считается прокрученной до конца. */
const NEAR_BOTTOM_PX = 48;

/** Ответ ещё не начался: ни размышлений, ни текста; проверка готового ответа сюда не относится. */
function isWaiting(generation: Generation): generation is Generation & { phase: 'sending' | 'searching' | 'preparing' } {
  return generation.phase === 'sending' || generation.phase === 'searching' || generation.phase === 'preparing';
}

/** Через 20 секунд без текста и размышлений к строке состояния добавляется пояснение. */
function useBusy(generation: Generation | null): boolean {
  const waiting = generation && isWaiting(generation);
  const waitingSince = waiting ? generation.startedAt : null;
  const [busySince, setBusySince] = useState<number | null>(null);
  useEffect(() => {
    if (waitingSince === null) {
      return;
    }
    const timer = window.setTimeout(() => setBusySince(waitingSince), BUSY_AFTER_MS);
    return () => window.clearTimeout(timer);
  }, [waitingSince]);
  return waitingSince !== null && busySince === waitingSince;
}

/** Открыть просмотр источника; `openerId` — куда вернуть фокус (атрибут `data-opener`). */
type OpenSource = (source: Source, openerId: string) => void;

interface AnswerProps {
  message: ChatMessage;
  /** Ответ формируется в этой вкладке — только у последнего сообщения. */
  generation: Generation | null;
  isLast: boolean;
  /** Нет — лента только для чтения (прежний диалог помощника CoGIS): «Ответить заново» не показывается. */
  onRegenerate?: () => void;
  onOpenSource: OpenSource;
  openingFragment: string | null;
  dialogId: string;
  /** Своя строка раздела, когда поиск ничего не нашёл; без неё — тексты чата по тому, были ли у вопроса теги. */
  nothingFoundText?: string;
  /** «Продолжить отсюда» (0.8): перенос разговора до этого ответа; нет — функция выключена. */
  onContinue?: () => void;
  /** Своя строка состояния ответа (помощник проекта); `null` — как в чате. */
  phaseLabel?: PhaseLabel;
  /** Блок под текстом ответа — карточка правки помощника проекта (концепция §5.13.11). */
  extra?: React.ReactNode;
  /** Свой текст отказа модели по длине (помощник проекта, §5.13.10). */
  tooLongText?: string;
  /** «Проверить снова» блока проверки на образце (SQL-помощник, 1.0); `recheckWait` — почему ждёт. */
  onRecheck?: (index: number) => void;
  recheckWait?: string | null;
  /** Идёт «Проверить снова»: диалог занят, как во время ответа (концепция §5.7, А-143). */
  rechecking?: boolean;
}

/** Строка состояния своего раздела: по фазе и тексту, полученному к этому моменту. */
export type PhaseLabel = (generation: Generation, message: ChatMessage) => string | null;

type FigureSource = Source & { image_id: string };

/** Картинки источников под ответом: одна картинка — один раз, сколько бы сносок на неё ни было (концепция §4.2). */
function figuresOf(sources: Source[], broken: ReadonlySet<string>): FigureSource[] {
  const seen = new Set(broken);
  return sources.filter((source): source is FigureSource => {
    if (source.image_id === null || seen.has(source.image_id)) {
      return false;
    }
    seen.add(source.image_id);
    return true;
  });
}

/** Строка под ответом, когда поиск ничего не нашёл: были ли теги, известно только у ответа из этой вкладки. */
function nothingFoundText(askedWithTags: boolean | undefined): string {
  const found = t.sources.nothingFound;
  if (askedWithTags === undefined) {
    return found.fromHistory;
  }
  return askedWithTags ? found.withTags : found.withoutTags;
}

function Answer(props: AnswerProps) {
  const { message, generation, isLast, onRegenerate, onOpenSource, openingFragment, dialogId } = props;
  const copy = useCopy();
  const busy = useBusy(generation);
  const hasText = message.content.length > 0;
  const formingElsewhere = message.status === 'streaming' && !generation;
  // Проверка на тестовых данных и цикл исправления идут после ответа: он дописан, но диалог занят до их конца.
  const checking = generation?.phase === 'checking' || generation?.phase === 'fixing' || Boolean(props.rechecking);
  const ownLabel = generation && props.phaseLabel ? props.phaseLabel(generation, message) : null;
  const [activeSource, setActiveSource] = useState<number | null>(null);
  const [brokenFigures, setBrokenFigures] = useState<ReadonlySet<string>>(new Set());
  const answerId = useId();
  // Карточки и кнопки-сноски появляются по завершении ответа, уже отобранными (концепция §4.3).
  const sources = message.status === 'streaming' ? null : message.sources;
  const links = useMemo<SourceLinks | undefined>(
    () =>
      sources && sources.length > 0
        ? {
            sources,
            active: activeSource,
            openerId: (n) => `${answerId}-mark-${n}`,
            onActive: setActiveSource,
            onOpen: onOpenSource,
          }
        : undefined,
    [sources, activeSource, answerId, onOpenSource],
  );
  const figures = figuresOf(sources ?? [], brokenFigures);

  return (
    <div className={message.memory_mode === 'excluded' ? `${styles.answer} ${styles.excludedAnswer}` : styles.answer}>
      {ownLabel && generation && (isWaiting(generation) || generation.phase === 'fixing') && (
        <p className={styles.state}>
          <Spinner type="mini" caption={null} />
          <span>
            {ownLabel}
            {busy && isWaiting(generation) && ` ${t.busy}`}
          </span>
        </p>
      )}
      {!ownLabel && generation && isWaiting(generation) && (
        <p className={styles.state}>
          <Spinner type="mini" caption={null} />
          <span>
            {t[generation.phase]}
            {busy && ` ${t.busy}`}
          </span>
        </p>
      )}
      {/* Ответ, который формируется в другой вкладке, показан только индикатором (концепция §5.5). */}
      {message.reasoning && !formingElsewhere && (
        <ThinkingBlock
          text={message.reasoning}
          seconds={message.reasoning_seconds}
          startedAt={generation?.phase === 'thinking' ? generation.reasoningStartedAt : null}
        />
      )}
      {hasText && !formingElsewhere && (
        <MarkdownView
          text={message.content}
          streaming={generation !== null && !checking}
          links={links}
          sqlCheck={message.sql_check}
          sqlRun={message.sql_run}
          onRecheck={props.onRecheck}
          recheckWait={props.recheckWait}
        />
      )}
      {/* Строка «Готовлю правку…» — пока идёт текст ответа с блоком правки. */}
      {ownLabel && generation?.phase === 'answering' && (
        <p className={styles.state}>
          <Spinner type="mini" caption={null} />
          <span>{ownLabel}</span>
        </p>
      )}
      {props.extra}
      {figures.length > 0 && (
        <section className={styles.sources}>
          <h2 className={styles.sourcesTitle}>{t.sources.figures}</h2>
          <div className={styles.sourceCards}>
            {figures.map((source) => (
              <SourceFigure
                key={source.image_id}
                source={source}
                openerId={`${answerId}-figure-${source.n}`}
                onActive={setActiveSource}
                onOpen={() => onOpenSource(source, `${answerId}-figure-${source.n}`)}
                onFailed={() => setBrokenFigures((broken) => new Set(broken).add(source.image_id))}
              />
            ))}
          </div>
        </section>
      )}
      {sources && sources.length > 0 && (
        <section className={styles.sources}>
          <h2 className={styles.sourcesTitle}>{t.sources.title}</h2>
          <div className={styles.sourceCards}>
            {sources.map((source) => (
              <SourceCard
                key={source.n}
                source={source}
                active={activeSource === source.n}
                opening={openingFragment === source.fragment_id}
                openerId={`${answerId}-card-${source.n}`}
                onActive={setActiveSource}
                onOpen={() => onOpenSource(source, `${answerId}-card-${source.n}`)}
              />
            ))}
          </div>
        </section>
      )}
      {/* Ноль найденного виден сразу по событию `sources`; «нет ссылок» — по завершении ответа. */}
      {message.sources_found === 0 && (
        <p className={styles.note}>{props.nothingFoundText ?? nothingFoundText(message.asked_with_tags)}</p>
      )}
      {sources && sources.length === 0 && (message.sources_found ?? 0) > 0 && (
        <p className={styles.note}>{t.sources.notCited}</p>
      )}
      {formingElsewhere && (
        <p className={styles.state}>
          <Spinner type="mini" caption={null} />
          <span>{t.stillForming}</span>
        </p>
      )}
      {message.status === 'stopped' && <p className={styles.note}>{t.stopped}</p>}
      {message.status === 'length_limit' && (
        <p className={styles.note}>{hasText ? t.lengthLimit : t.lengthLimitEmpty}</p>
      )}
      {message.status === 'error' && (
        <Notice
          kind="error"
          action={isLast && onRegenerate && !props.rechecking ? { label: texts.common.retry, onClick: onRegenerate } : undefined}
        >
          {message.error_code === 'message_too_long' && props.tooLongText
            ? props.tooLongText
            : answerErrorText(message.error_code, hasText, message.error_message)}
        </Notice>
      )}
      {message.status !== 'streaming' && message.status !== 'error' && (
        <div className={styles.actions}>
          {hasText && (
            <Link component="button" icon={<IconCopyRegular16 />} onClick={() => void copy.copy(message.content)}>
              {copy.label}
            </Link>
          )}
          {isLast && onRegenerate && !checking && (
            <Link component="button" icon={<IconArrowRoundSyncForwardRegular16 />} onClick={onRegenerate}>
              {t.regenerate}
            </Link>
          )}
          {isLast && onRegenerate && checking && (
            // Недоступное действие остаётся в обходе по Tab и объясняет причину, а не молчит.
            <TitleHint
              text={
                props.rechecking
                  ? texts.db.run.recheckBusy
                  : generation?.phase === 'fixing'
                    ? texts.workshop.assistant.busy
                    : texts.sql.testRun.waitRun
              }
              always
            >
              <Link
                component="button"
                className={styles.unavailable}
                aria-disabled="true"
                icon={<IconArrowRoundSyncForwardRegular16 />}
              >
                {t.regenerate}
              </Link>
            </TitleHint>
          )}
          {hasText && (
            <Link
              component="button"
              icon={<IconNetDownloadRegular16 />}
              onClick={() => void exportDocx(dialogId, message.id)}
            >
              {t.exportAnswer}
            </Link>
          )}
          {hasText && props.onContinue && (
            <Link component="button" icon={<IconArrowDCornerDownRightRegular16 />} onClick={props.onContinue}>
              {texts.memory.transfer.continue}
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

interface MemoryMarkProps {
  text: string;
  label?: string;
  icon?: React.ReactElement;
  /** Открыть панель памяти на этом вопросе; `null` — функция выключена, пометка — обычный текст (Д-36). */
  onOpen: (() => void) | null;
}

/** Пометка выбора пары в ленте (концепция §4.9, «Лента»): читается перед вопросом или под вложениями. */
function MemoryMark({ text, label, icon, onOpen }: MemoryMarkProps) {
  return (
    <p className={styles.memoryMark}>
      {onOpen ? (
        <Link className={styles.memoryMarkLink} component="button" use="grayed" icon={icon} aria-label={label} onClick={onOpen}>
          {text}
        </Link>
      ) : (
        <>
          {icon && (
            <span className={styles.memoryMarkIcon} aria-hidden="true">
              {icon}
            </span>
          )}
          {text}
        </>
      )}
    </p>
  );
}

const NO_MEMORY: FeedMemory = { onOpenPair: null, marked: null, reveal: null, revealing: false, onContinue: null };

/** Что сообщает экранному чтению область состояния: фазы, а не текст ответа по словам (концепция §9). */
function phaseText(view: DialogView): string {
  if (view.generation) {
    const phases = {
      sending: t.sending,
      searching: t.phase.searching,
      preparing: t.phase.preparing,
      thinking: t.phase.thinking,
      answering: t.phase.answering,
      // Шаги проверки не озвучиваются: её итог объявляется один раз, по завершении.
      checking: t.phase.done,
      fixing: t.phase.done,
    };
    return phases[view.generation.phase];
  }
  const last = view.messages.at(-1);
  if (last?.role !== 'assistant') {
    return '';
  }
  if (last.status === 'stopped') {
    return t.phase.stopped;
  }
  if (last.sql_run) {
    // Проверять было нечего или у блоков нет объявления — обычное «Ответ готов».
    return sqlRunAnnouncement(last.sql_run) || t.phase.done;
  }
  if (last.code_edit && last.code_edit.files.length > 0) {
    // Приход правки помощника раздела «Код» объявляется вместо «Ответ готов» (концепция §9).
    return texts.workshop.edit.announce(last.code_edit.files.length);
  }
  return last.status === 'complete' || last.status === 'length_limit' ? t.phase.done : '';
}

interface MessageListProps {
  dialogId: string;
  view: DialogView;
  /** Нет — лента только для чтения (прежний диалог помощника CoGIS): «Ответить заново» не показывается. */
  onRegenerate?: () => void;
  onLoadOlder: () => void;
  onOpenSource: OpenSource;
  /** Цитата, просмотр которой сейчас открывается: на её карточке — индикатор. */
  openingFragment: string | null;
  /** Строка под ответом, когда поиск ничего не нашёл; по умолчанию — как в чате. */
  nothingFoundText?: string;
  /** Содержимое над сообщениями в той же прокрутке (результат разбора документа). */
  header?: React.ReactNode;
  /** Память диалога: пометки пар ведут в панель, пара из описи отмечается и прокручивается. */
  memory?: FeedMemory;
  /** Узкая колонка (помощник проекта, Д-57): «Вы» и «Ответ» — над блоком, а не на полях. */
  narrow?: boolean;
  /** Строка состояния ответа своего раздела. */
  phaseLabel?: PhaseLabel;
  /** Под вопросом (фрагмент кода) и под ответом (карточка правки) — свои блоки раздела. */
  questionExtra?: (message: ChatMessage) => React.ReactNode;
  answerExtra?: (message: ChatMessage, isLast: boolean) => React.ReactNode;
  tooLongText?: string;
  /** «Проверить снова» у сохранённых блоков проверки на образце (SQL-помощник, 1.0). */
  onRecheck?: (messageId: string, index: number) => void;
}

/** Лента-протокол (концепция §4.2): слева пометка «Вы» или «Ответ», вопрос — на подложке, ответ — текстом. */
export function MessageList(props: MessageListProps) {
  const { dialogId, view, onRegenerate, onLoadOlder, onOpenSource, openingFragment } = props;
  const memory = props.memory ?? NO_MEMORY;
  const { reveal } = memory;
  const narrow = props.narrow ?? false;
  // «Проверить снова» ждёт конца ответа или другой проверки этого диалога (концепция §5.7).
  const recheckWait =
    view.generation || view.recheck || view.messages.at(-1)?.status === 'streaming' ? texts.db.run.recheckWait : null;
  const scroll = useRef<ScrollContainer>(null);
  const list = useRef<HTMLDivElement>(null);
  const [following, setFollowing] = useState(true);
  const [image, setImage] = useState<Attachment | null>(null);
  const rememberOpener = useFocusReturn(image !== null);
  const last = view.messages.at(-1);

  // Лента следует за ответом, пока пользователь сам не прокрутил вверх.
  useEffect(() => {
    if (following) {
      scroll.current?.scrollToBottom();
    }
  }, [following, view.messages.length, last?.content, last?.reasoning, last?.status, last?.sql_run, last?.code_edit]);

  // Высота ленты меняется и без новых сообщений (настройки в заголовке перенеслись на вторую строку):
  // конец ответа не должен уйти под панель запроса.
  useEffect(() => {
    const area = list.current;
    if (!following || !area || typeof ResizeObserver === 'undefined') {
      return;
    }
    const observer = new ResizeObserver(() => scroll.current?.scrollToBottom());
    observer.observe(area);
    return () => observer.disconnect();
  }, [following]);

  // Переход из описи памяти: лента прокручивается к вопросу и больше не следует за ответом.
  useEffect(() => {
    if (!reveal) {
      return;
    }
    const row = list.current?.querySelector(`[data-message-id="${reveal.questionId}"]`);
    if (row) {
      setFollowing(false);
      row.scrollIntoView({ block: 'start' });
      // Узкая колонка: панель памяти закрылась, фокус — на блок вопроса (концепция §5.13.12).
      if (narrow && row instanceof HTMLElement) {
        row.tabIndex = -1;
        row.focus({ preventScroll: true });
      }
    }
  }, [reveal, narrow]);

  return (
    <div ref={list} className={props.narrow ? `${styles.list} ${styles.narrow}` : styles.list}>
      <ScrollContainer
        ref={scroll}
        onScroll={(event) => {
          const { scrollHeight, scrollTop, clientHeight } = event.currentTarget;
          setFollowing(scrollHeight - scrollTop - clientHeight < NEAR_BOTTOM_PX);
        }}
      >
        <div className={styles.column}>
          {props.header}
          {view.olderCursor && (
            <p className={styles.earlier}>
              <Link component="button" onClick={onLoadOlder}>
                {t.earlier}
              </Link>
            </p>
          )}
          {view.messages.map((message, index) => {
            const previous = view.messages[index - 1];
            const pairId = message.role === 'user' ? message.id : previous?.role === 'user' ? previous.id : null;
            const marked = pairId !== null && pairId === memory.marked;
            const openPair = memory.onOpenPair && pairId ? () => memory.onOpenPair?.(pairId) : null;
            const { onContinue } = memory;
            return (
            <Fragment key={message.id}>
            <div
              data-message-id={message.id}
              className={marked ? `${styles.row} ${styles.marked}` : styles.row}
            >
              <div className={styles.who}>{message.role === 'user' ? t.you : t.answer}</div>
              {message.role === 'user' ? (
                <div className={styles.questionColumn}>
                {message.memory_mode === 'excluded' && (
                  <MemoryMark
                    text={texts.memory.excluded}
                    label={texts.memory.excludedLabel}
                    icon={<IconEyeOffRegular16 />}
                    onOpen={openPair}
                  />
                )}
                <div
                  className={
                    message.memory_mode === 'excluded' ? `${styles.question} ${styles.questionExcluded}` : styles.question
                  }
                >
                  {message.content && <p className={styles.questionText}>{message.content}</p>}
                  {message.attachments.length > 0 && (
                    <div className={styles.attachments}>
                      {message.attachments.map((attachment) => (
                        <span key={attachment.id} data-opener={attachment.id}>
                          <AttachmentChip
                            name={attachment.file_name}
                            attachment={attachment}
                            fileUrl={attachmentFileUrl(dialogId, attachment.id)}
                            onOpenImage={
                              isImage(attachment)
                                ? () => {
                                    rememberOpener(`[data-opener="${attachment.id}"] button`);
                                    setImage(attachment);
                                  }
                                : undefined
                            }
                          />
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                {message.memory_mode === 'text_only' && message.attachments.length > 0 && (
                  <MemoryMark text={texts.memory.textOnly} onOpen={openPair} />
                )}
                {props.questionExtra?.(message)}
                {/* Опасные операции во вставленном запросе — под вопросом, на всю ширину колонки (§5.7). */}
                {message.sql_dangers?.map((danger) => (
                  <Notice key={danger} kind="warning">
                    {texts.sql.dangers[danger]}
                  </Notice>
                ))}
                </div>
              ) : (
                <Answer
                  message={message}
                  generation={index === view.messages.length - 1 ? view.generation : null}
                  isLast={index === view.messages.length - 1}
                  onRegenerate={onRegenerate}
                  onOpenSource={onOpenSource}
                  openingFragment={openingFragment}
                  dialogId={dialogId}
                  nothingFoundText={props.nothingFoundText}
                  onContinue={onContinue ? () => onContinue(message.id) : undefined}
                  phaseLabel={props.phaseLabel}
                  tooLongText={props.tooLongText}
                  extra={props.answerExtra?.(message, index === view.messages.length - 1)}
                  onRecheck={props.onRecheck && ((block) => props.onRecheck?.(message.id, block))}
                  recheckWait={recheckWait}
                  rechecking={view.recheck !== null}
                />
              )}
            </div>
            {/* Граница пересказа — сразу после последнего пересказанного сообщения (0.8, §4.9). */}
            {view.summary?.until_message_id === message.id && <SummaryBoundary summary={view.summary} />}
            </Fragment>
            );
          })}
        </div>
      </ScrollContainer>
      {memory.revealing && (
        <div className={styles.revealing}>
          <Loader active caption={texts.common.loading}>
            <div className={styles.revealingBox} />
          </Loader>
        </div>
      )}
      <p className="p-visually-hidden" role="status">
        {phaseText(view)}
      </p>
      {!following && (
        <div className={styles.toLast}>
          <Button
            icon={<IconArrowADownRegular16 />}
            onClick={() => {
              scroll.current?.scrollToBottom();
              setFollowing(true);
            }}
          >
            {t.toLast}
          </Button>
        </div>
      )}
      {image && (
        <Modal onClose={() => setImage(null)}>
          <Modal.Header>{image.file_name}</Modal.Header>
          <Modal.Body>
            <img className={styles.image} src={attachmentFileUrl(dialogId, image.id)} alt={image.file_name} />
          </Modal.Body>
        </Modal>
      )}
    </div>
  );
}
