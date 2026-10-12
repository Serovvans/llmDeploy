import { IconCheckARegular16 } from '@skbkontur/icons/IconCheckARegular16';
import { IconTimeClockRegular16 } from '@skbkontur/icons/IconTimeClockRegular16';
import { IconWarningCircleRegular16 } from '@skbkontur/icons/IconWarningCircleRegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Link, Spinner } from '@skbkontur/react-ui';

import type { DraftAttachment } from '../../chat/state';
import { formatDuration, texts } from '../../texts';
import { ProgressBar } from '../ProgressBar';
import styles from './AttachmentChip.module.css';

const t = texts.chat.media;

interface MediaAttachmentChipProps {
  item: DraftAttachment & { media: NonNullable<DraftAttachment['media']> };
  onRetry: () => void;
  onRemove: () => void;
}

interface MediaState {
  icon: React.ReactNode;
  /** Состояние словом: его смена объявляется экранному чтению один раз. */
  word: React.ReactNode;
  /** Ход и длительность: читаются обычной навигацией, вслух сами не объявляются (концепция §9). */
  detail?: string;
  failed?: boolean;
}

function mediaState({ status, media, attachment }: MediaAttachmentChipProps['item']): MediaState {
  const spinner = <Spinner type="mini" caption={null} />;
  const clock = <IconTimeClockRegular16 aria-hidden="true" />;
  if (status === 'failed') {
    return { icon: <IconWarningCircleRegular16 aria-hidden="true" />, word: media.failure?.text, failed: true };
  }
  if (status === 'waiting') {
    return { icon: clock, word: t.waiting };
  }
  if (status === 'uploading') {
    return { icon: spinner, word: t.uploading, detail: t.percent(media.percent) };
  }
  if (attachment?.transcription?.status === 'processing') {
    return { icon: spinner, word: t.processing };
  }
  if (attachment?.transcription?.status === 'ready') {
    const duration = attachment.duration_seconds;
    return {
      icon: <IconCheckARegular16 aria-hidden="true" />,
      word: (
        <>
          <span className="p-visually-hidden">{t.readyAnnounce}</span>
          <span aria-hidden="true">{t.ready}</span>
        </>
      ),
      detail: duration === null ? undefined : `· ${formatDuration(duration)}`,
    };
  }
  return { icon: clock, word: t.queued };
}

/**
 * Запись (аудио, видео) в панели запроса (концепция §5.5): имя и строка состояния — значок и слово,
 * не только цвет; во время загрузки — полоса хода. Крестик убирает запись из очереди, отменяет загрузку
 * или удаляет загруженное вложение.
 */
export function MediaAttachmentChip({ item, onRetry, onRemove }: MediaAttachmentChipProps) {
  const name = item.file.name;
  const state = mediaState(item);
  return (
    <span className={`${styles.chip} ${styles.media}`}>
      <span className={styles.mediaBody}>
        <span className={styles.name}>{name}</span>
        <span className={styles.mediaState}>
          <span className={state.failed ? styles.failed : styles.mediaIcon}>{state.icon}</span>
          <span>
            <span role="status">{state.word}</span>
            {state.detail && ` ${state.detail}`}
            {item.media.failure?.retryable && (
              <>
                {' '}
                <Link component="button" onClick={onRetry}>
                  {texts.common.retry}
                </Link>
              </>
            )}
          </span>
        </span>
        {item.status === 'uploading' && <ProgressBar percent={item.media.percent} />}
      </span>
      <button type="button" className={styles.remove} aria-label={texts.chat.files.remove(name)} onClick={onRemove}>
        <IconXRegular16 aria-hidden="true" />
      </button>
    </span>
  );
}
