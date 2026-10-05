import { IconDocTextRegular16 } from '@skbkontur/icons/IconDocTextRegular16';
import { IconWarningCircleRegular16 } from '@skbkontur/icons/IconWarningCircleRegular16';
import { IconXRegular16 } from '@skbkontur/icons/IconXRegular16';
import { Link, Spinner } from '@skbkontur/react-ui';
import { useState } from 'react';

import type { Attachment } from '../../api/types';
import { texts } from '../../texts';
import styles from './AttachmentChip.module.css';

interface AttachmentChipProps {
  name: string;
  /** Загруженное вложение; `null`, пока файл загружается или загрузка оборвалась. */
  attachment: Attachment | null;
  /** Адрес файла на портале — для миниатюры изображения. */
  fileUrl: string | null;
  failed?: boolean;
  onRetry?: () => void;
  onRemove?: () => void;
  onOpenImage?: () => void;
}

export function isImage(attachment: Attachment): boolean {
  return attachment.media_type.startsWith('image/');
}

const THUMBNAIL_PX = 48;

/**
 * Миниатюра — оригинал файла в рамке 48×48 (концепция §4.1): запрашивается, когда сообщение подходит
 * к видимой области; заданные размеры не дают ленте прыгать. Не загрузившаяся заменяется значком файла.
 */
function Thumbnail({ src }: { src: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <span className={styles.thumbnailFailed} aria-hidden="true">
        <IconDocTextRegular16 />
      </span>
    );
  }
  return (
    <img
      className={styles.thumbnail}
      src={src}
      alt=""
      width={THUMBNAIL_PX}
      height={THUMBNAIL_PX}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}

/** Вложение в панели запроса и в сообщении (концепция §5.5): имя, число страниц PDF, миниатюра изображения. */
export function AttachmentChip({ name, attachment, fileUrl, failed, onRetry, onRemove, onOpenImage }: AttachmentChipProps) {
  const image = attachment && isImage(attachment) && fileUrl;
  const pages = attachment?.media_type === 'application/pdf' ? attachment.page_count : null;
  const label = (
    <>
      <span className={styles.name}>{name}</span>
      {pages !== null && <span className={styles.pages}> · {texts.chat.files.pages(pages)}</span>}
    </>
  );

  return (
    <span className={styles.chip}>
      {image && onOpenImage ? (
        <button type="button" className={styles.open} onClick={onOpenImage}>
          <Thumbnail src={fileUrl} />
          {label}
        </button>
      ) : (
        <>
          {image && <Thumbnail src={fileUrl} />}
          {!attachment && !failed && <Spinner type="mini" caption={null} />}
          {failed && (
            <span className={styles.failed} aria-hidden="true">
              <IconWarningCircleRegular16 />
            </span>
          )}
          {label}
        </>
      )}
      {failed && onRetry && (
        <Link component="button" onClick={onRetry}>
          {texts.common.retry}
        </Link>
      )}
      {onRemove && (
        <button type="button" className={styles.remove} aria-label={texts.chat.files.remove(name)} onClick={onRemove}>
          <IconXRegular16 aria-hidden="true" />
        </button>
      )}
    </span>
  );
}
