import { IconAttachPaperclipRegular16 } from '@skbkontur/icons/IconAttachPaperclipRegular16';
import { IconMediaUiAStopRegular16 } from '@skbkontur/icons/IconMediaUiAStopRegular16';
import { Button, Hint, Switcher, Textarea } from '@skbkontur/react-ui';
import { useEffect, useId, useRef } from 'react';

import { attachmentFileUrl } from '../../api/client';
import type { AnswerMode } from '../../api/types';
import type { Draft } from '../../chat/state';
import { texts } from '../../texts';
import { AttachmentChip } from './AttachmentChip';
import styles from './Composer.module.css';

const t = texts.chat.composer;

const MODES = [
  { value: 'fast', label: t.modeFast },
  { value: 'thorough', label: t.modeThorough },
];

interface ComposerProps {
  /** Чат, к которому относятся загруженные вложения; `null`, пока чат не создан. */
  dialogId: string | null;
  draft: Draft;
  mode: AnswerMode;
  /** `generating` — ответ идёт в этой вкладке (есть «Остановить»); `waiting` — формируется в другой. */
  answer: 'idle' | 'generating' | 'waiting';
  /** Подсказка на кнопке «Прикрепить» и расширения для выбора файла; `null`, пока нет конфигурации. */
  attach: { hint: string; extensions: string[] } | null;
  onTextChange: (text: string) => void;
  onModeChange: (mode: AnswerMode) => void;
  onSend: () => void;
  onStop: () => void;
  onNotice: (notice: string) => void;
  onAddFiles: (files: File[]) => void;
  onRetryFile: (key: string) => void;
  onRemoveFile: (key: string) => void;
}

/** Панель запроса (концепция §4.1): поле, вложения, режим ответа, «Отправить» / «Остановить». */
export function Composer(props: ComposerProps) {
  const { dialogId, draft, answer } = props;
  const textarea = useRef<Textarea>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const noticeId = useId();
  const hintId = useId();
  const busy = answer !== 'idle';

  // Фокус — в поле ввода: при открытии чата и после отправки.
  useEffect(() => {
    textarea.current?.focus();
  }, [dialogId]);

  const send = () => {
    textarea.current?.focus();
    if (busy) {
      props.onNotice(t.waitAnswer);
    } else {
      props.onSend();
    }
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      send();
    } else if (event.key === 'Escape' && answer === 'generating') {
      props.onStop();
    }
  };

  const onPaste = (event: React.ClipboardEvent) => {
    const images = Array.from(event.clipboardData.files).filter((file) => file.type.startsWith('image/'));
    if (images.length > 0) {
      event.preventDefault();
      props.onAddFiles(images);
    }
  };

  const onFilesChosen = (event: React.ChangeEvent<HTMLInputElement>) => {
    props.onAddFiles(Array.from(event.target.files ?? []));
    event.target.value = '';
  };

  return (
    <div className={styles.composer}>
      <div className={styles.box}>
        {draft.attachments.length > 0 && (
          <div className={styles.attachments}>
            {draft.attachments.map((item) => (
              <AttachmentChip
                key={item.key}
                name={item.file.name}
                attachment={item.attachment}
                fileUrl={dialogId && item.attachment ? attachmentFileUrl(dialogId, item.attachment.id) : null}
                failed={item.status === 'failed'}
                onRetry={() => props.onRetryFile(item.key)}
                onRemove={() => props.onRemoveFile(item.key)}
              />
            ))}
          </div>
        )}
        <Textarea
          ref={textarea}
          width="100%"
          autoResize
          rows={2}
          maxRows={10}
          extraRow={false}
          placeholder={t.placeholder}
          aria-label={t.placeholder}
          aria-describedby={`${hintId} ${draft.notice ? noticeId : ''}`.trim()}
          value={draft.text}
          onValueChange={props.onTextChange}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        <div className={styles.controls}>
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            accept={props.attach?.extensions.join(',')}
            onChange={onFilesChosen}
          />
          <Hint text={props.attach?.hint ?? ''} maxWidth={320}>
            <Button
              icon={<IconAttachPaperclipRegular16 />}
              disabled={!props.attach}
              onClick={() => fileInput.current?.click()}
            >
              {t.attach}
            </Button>
          </Hint>
          <Hint text={t.modeHint} maxWidth={320}>
            <Switcher
              items={MODES}
              value={props.mode}
              onValueChange={(value) => props.onModeChange(value === 'thorough' ? 'thorough' : 'fast')}
            />
          </Hint>
          <span className={styles.spacer} />
          {answer === 'generating' ? (
            <Button icon={<IconMediaUiAStopRegular16 />} onClick={props.onStop}>
              {t.stop}
            </Button>
          ) : (
            <Button use="primary" onClick={send}>
              {t.send}
            </Button>
          )}
        </div>
      </div>
      <p id={hintId} className={styles.hint}>
        {t.hint}
      </p>
      {draft.notice && (
        <p id={noticeId} className={styles.notice} role="alert">
          {draft.notice}
        </p>
      )}
    </div>
  );
}
