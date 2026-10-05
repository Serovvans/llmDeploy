import { IconArrowADownRegular16 } from '@skbkontur/icons/IconArrowADownRegular16';
import { IconArrowRoundSyncForwardRegular16 } from '@skbkontur/icons/IconArrowRoundSyncForwardRegular16';
import { IconCopyRegular16 } from '@skbkontur/icons/IconCopyRegular16';
import { Button, Link, Modal, ScrollContainer, Spinner } from '@skbkontur/react-ui';
import { useEffect, useRef, useState } from 'react';

import { attachmentFileUrl } from '../../api/client';
import type { Attachment } from '../../api/types';
import { answerErrorText } from '../../chat/files';
import type { ChatMessage, DialogView, Generation } from '../../chat/state';
import { useCopy } from '../../hooks/useCopy';
import { useFocusReturn } from '../../hooks/useFocusReturn';
import { texts } from '../../texts';
import { Notice } from '../Notice';
import { AttachmentChip, isImage } from './AttachmentChip';
import { MarkdownView } from './MarkdownView';
import styles from './MessageList.module.css';
import { ThinkingBlock } from './ThinkingBlock';

const t = texts.chat;
const BUSY_AFTER_MS = 20_000;
/** Ближе этого к нижнему краю лента считается прокрученной до конца. */
const NEAR_BOTTOM_PX = 48;

/** Через 20 секунд без текста и размышлений к строке состояния добавляется пояснение. */
function useBusy(generation: Generation | null): boolean {
  const waitingSince = generation?.phase === 'sending' ? generation.startedAt : null;
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

interface AnswerProps {
  message: ChatMessage;
  /** Ответ формируется в этой вкладке — только у последнего сообщения. */
  generation: Generation | null;
  isLast: boolean;
  onRegenerate: () => void;
}

function Answer({ message, generation, isLast, onRegenerate }: AnswerProps) {
  const copy = useCopy();
  const busy = useBusy(generation);
  const hasText = message.content.length > 0;
  const formingElsewhere = message.status === 'streaming' && !generation;

  return (
    <div className={styles.answer}>
      {generation?.phase === 'sending' && (
        <p className={styles.state}>
          <Spinner type="mini" caption={null} />
          <span>
            {t.sending}
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
      {hasText && !formingElsewhere && <MarkdownView text={message.content} streaming={generation !== null} />}
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
        <Notice kind="error" action={isLast ? { label: texts.common.retry, onClick: onRegenerate } : undefined}>
          {answerErrorText(message.error_code, hasText, message.error_message)}
        </Notice>
      )}
      {message.status !== 'streaming' && message.status !== 'error' && (
        <div className={styles.actions}>
          {hasText && (
            <Link component="button" icon={<IconCopyRegular16 />} onClick={() => void copy.copy(message.content)}>
              {copy.label}
            </Link>
          )}
          {isLast && (
            <Link component="button" icon={<IconArrowRoundSyncForwardRegular16 />} onClick={onRegenerate}>
              {t.regenerate}
            </Link>
          )}
        </div>
      )}
    </div>
  );
}

/** Что сообщает экранному чтению область состояния: фазы, а не текст ответа по словам (концепция §9). */
function phaseText(view: DialogView): string {
  if (view.generation) {
    const phases = { sending: t.sending, thinking: t.phase.thinking, answering: t.phase.answering };
    return phases[view.generation.phase];
  }
  const last = view.messages.at(-1);
  if (last?.role !== 'assistant') {
    return '';
  }
  if (last.status === 'stopped') {
    return t.phase.stopped;
  }
  return last.status === 'complete' || last.status === 'length_limit' ? t.phase.done : '';
}

interface MessageListProps {
  dialogId: string;
  view: DialogView;
  onRegenerate: () => void;
  onLoadOlder: () => void;
}

/** Лента-протокол (концепция §4.2): слева пометка «Вы» или «Ответ», вопрос — на подложке, ответ — текстом. */
export function MessageList({ dialogId, view, onRegenerate, onLoadOlder }: MessageListProps) {
  const scroll = useRef<ScrollContainer>(null);
  const [following, setFollowing] = useState(true);
  const [image, setImage] = useState<Attachment | null>(null);
  const rememberOpener = useFocusReturn(image !== null);
  const last = view.messages.at(-1);

  // Лента следует за ответом, пока пользователь сам не прокрутил вверх.
  useEffect(() => {
    if (following) {
      scroll.current?.scrollToBottom();
    }
  }, [following, view.messages.length, last?.content, last?.reasoning, last?.status]);

  return (
    <div className={styles.list}>
      <ScrollContainer
        ref={scroll}
        onScroll={(event) => {
          const { scrollHeight, scrollTop, clientHeight } = event.currentTarget;
          setFollowing(scrollHeight - scrollTop - clientHeight < NEAR_BOTTOM_PX);
        }}
      >
        <div className={styles.column}>
          {view.olderCursor && (
            <p className={styles.earlier}>
              <Link component="button" onClick={onLoadOlder}>
                {t.earlier}
              </Link>
            </p>
          )}
          {view.messages.map((message, index) => (
            <div key={message.id} className={styles.row}>
              <div className={styles.who}>{message.role === 'user' ? t.you : t.answer}</div>
              {message.role === 'user' ? (
                <div className={styles.question}>
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
              ) : (
                <Answer
                  message={message}
                  generation={index === view.messages.length - 1 ? view.generation : null}
                  isLast={index === view.messages.length - 1}
                  onRegenerate={onRegenerate}
                />
              )}
            </div>
          ))}
        </div>
      </ScrollContainer>
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
