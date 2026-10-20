import { IconAttachPaperclipRegular16 } from '@skbkontur/icons/IconAttachPaperclipRegular16';
import { IconMediaUiAStopRegular16 } from '@skbkontur/icons/IconMediaUiAStopRegular16';
import { Button, Hint, Switcher, Textarea } from '@skbkontur/react-ui';
import { useEffect, useId, useRef } from 'react';

import { attachmentFileUrl } from '../../api/client';
import type { AnswerMode, KbScope } from '../../api/types';
import type { Draft } from '../../chat/state';
import { texts } from '../../texts';
import { AttachmentChip } from './AttachmentChip';
import styles from './Composer.module.css';
import { KnowledgePicker } from './KnowledgePicker';
import { MediaAttachmentChip } from './MediaAttachmentChip';

const t = texts.chat.composer;

const MODES = [
  { value: 'fast', label: t.modeFast },
  { value: 'thorough', label: t.modeThorough },
];

/** Вложения, режим ответа и база знаний — только в чате (концепция §4.1). */
interface ChatControls {
  mode: AnswerMode;
  collections: KbScope[];
  tags: string[];
  /** Предел числа тегов в вопросе; `null`, пока нет конфигурации. */
  maxTags: number | null;
  /** Подсказка на кнопке «Прикрепить» и расширения для выбора файла; `null`, пока нет конфигурации. */
  attach: { hint: string; extensions: string[] } | null;
  onModeChange: (mode: AnswerMode) => void;
  onCollectionsChange: (collections: KbScope[]) => void;
  onTagsChange: (tags: string[]) => void;
  onAddFiles: (files: File[]) => void;
  onRetryFile: (key: string) => void;
  onRemoveFile: (key: string) => void;
}

interface ComposerProps {
  /** Диалог, к которому относятся загруженные вложения; `null`, пока он не создан. */
  dialogId: string | null;
  draft: Draft;
  /** `generating` — ответ идёт в этой вкладке (есть «Остановить»); `waiting` — формируется в другой. */
  answer: 'idle' | 'generating' | 'waiting';
  /** Подсказка в поле ввода; по умолчанию — как в чате. */
  placeholder?: string;
  /** Поле набирается моноширинным шрифтом — режимы со вставкой запроса или кода. */
  mono?: boolean;
  /** Что сказать под полем при попытке отправить во время ответа. */
  waitText?: string;
  /** Постоянная строка под панелью — что портал не делает. */
  note?: string;
  /** Отправка пока невозможна (настройки ещё загружаются): кнопка «Отправить» — в состоянии `loading`. */
  sendPending?: boolean;
  chat?: ChatControls;
  /** Свой элемент строки управления слева, на месте режима ответа чата: действие SQL-помощника. */
  controls?: React.ReactNode;
  /** Индикатор памяти диалога — справа в строке подсказки, под кнопкой «Отправить» (концепция §4.1, Д-28). */
  meter?: React.ReactNode;
  onTextChange: (text: string) => void;
  onSend: () => void;
  onStop: () => void;
  onNotice: (notice: string) => void;
}

/** Панель запроса (концепция §4.1) — одна на всех рабочих экранах: поле, «Отправить» / «Остановить». */
export function Composer(props: ComposerProps) {
  const { dialogId, draft, answer, chat } = props;
  const placeholder = props.placeholder ?? t.placeholder;
  const textarea = useRef<Textarea>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const noticeId = useId();
  const hintId = useId();
  const busy = answer !== 'idle';

  // Фокус — в поле ввода: при открытии диалога и после отправки.
  useEffect(() => {
    textarea.current?.focus();
  }, [dialogId]);

  const send = () => {
    textarea.current?.focus();
    if (busy) {
      props.onNotice(props.waitText ?? t.waitAnswer);
    } else if (!props.sendPending) {
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
    if (chat && images.length > 0) {
      event.preventDefault();
      chat.onAddFiles(images);
    }
  };

  const onFilesChosen = (event: React.ChangeEvent<HTMLInputElement>) => {
    chat?.onAddFiles(Array.from(event.target.files ?? []));
    event.target.value = '';
  };

  return (
    <div className={styles.composer} data-composer="">
      <div className={styles.box}>
        {draft.attachments.length > 0 && (
          <div className={styles.attachments}>
            {draft.attachments.map((item) =>
              item.media ? (
                <MediaAttachmentChip
                  key={item.key}
                  item={{ ...item, media: item.media }}
                  onRetry={() => chat?.onRetryFile(item.key)}
                  onRemove={() => chat?.onRemoveFile(item.key)}
                />
              ) : (
                <AttachmentChip
                  key={item.key}
                  name={item.file.name}
                  attachment={item.attachment}
                  fileUrl={dialogId && item.attachment ? attachmentFileUrl(dialogId, item.attachment.id) : null}
                  failed={item.status === 'failed'}
                  onRetry={() => chat?.onRetryFile(item.key)}
                  onRemove={() => chat?.onRemoveFile(item.key)}
                />
              ),
            )}
          </div>
        )}
        <Textarea
          ref={textarea}
          width="100%"
          autoResize
          rows={2}
          maxRows={10}
          extraRow={false}
          placeholder={placeholder}
          aria-label={placeholder}
          className={props.mono ? 'p-mono' : undefined}
          aria-describedby={`${hintId} ${draft.notice ? noticeId : ''}`.trim()}
          value={draft.text}
          onValueChange={props.onTextChange}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
        />
        <div className={styles.controls}>
          {chat && (
            <>
              <input
                ref={fileInput}
                type="file"
                multiple
                hidden
                accept={chat.attach?.extensions.join(',')}
                onChange={onFilesChosen}
              />
              <Hint text={chat.attach?.hint ?? ''} maxWidth={320}>
                <Button
                  icon={<IconAttachPaperclipRegular16 />}
                  onClick={() => fileInput.current?.click()}
                >
                  {t.attach}
                </Button>
              </Hint>
              <Hint text={t.modeHint} maxWidth={320}>
                <Switcher
                  items={MODES}
                  value={chat.mode}
                  onValueChange={(value) => chat.onModeChange(value === 'thorough' ? 'thorough' : 'fast')}
                />
              </Hint>
              <KnowledgePicker
                collections={chat.collections}
                tags={chat.tags}
                maxTags={chat.maxTags}
                onCollectionsChange={chat.onCollectionsChange}
                onTagsChange={chat.onTagsChange}
              />
            </>
          )}
          {props.controls}
          {/* Кнопка остаётся у правого края и тогда, когда строка управления перенеслась (концепция §4.1). */}
          <span className={styles.submit}>
            {answer === 'generating' ? (
              <Button icon={<IconMediaUiAStopRegular16 />} onClick={props.onStop}>
                {t.stop}
              </Button>
            ) : (
              <Button use="primary" loading={props.sendPending} onClick={send}>
                {t.send}
              </Button>
            )}
          </span>
        </div>
      </div>
      <div className={styles.hintRow}>
        <p id={hintId} className={styles.hint}>
          {t.hint}
        </p>
        {props.meter}
      </div>
      {props.note && <p className={styles.hint}>{props.note}</p>}
      {draft.notice && (
        <p id={noticeId} className={styles.notice} role="alert">
          {draft.notice}
        </p>
      )}
    </div>
  );
}
