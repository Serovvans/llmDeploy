import { IconMediaAudioVolumeARegular16 } from '@skbkontur/icons/IconMediaAudioVolumeARegular16';
import { Spinner } from '@skbkontur/react-ui';
import { useState } from 'react';

import { kbImageUrl } from '../../api/client';
import type { Source } from '../../api/types';
import { formatTimecode, texts } from '../../texts';
import { TitleHint } from '../TitleHint';
import styles from './SourceCard.module.css';

const t = texts.chat.sources;

interface SourceCardProps {
  source: Source;
  active: boolean;
  /** Просмотр этой цитаты открывается: до ответа сервера — индикатор, чтобы нажатие не казалось пропущенным. */
  opening: boolean;
  openerId: string;
  onActive: (n: number | null) => void;
  onOpen: () => void;
}

const SCOPE_LABELS: Partial<Record<Source['scope'], string>> = { personal: t.mine, ministries: t.ministries };

/** Картинка источника на месте цитаты; не загрузилась — строка «Картинка недоступна», карточка остаётся кнопкой. */
function CardImage({ source, imageId }: { source: Source; imageId: string }) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return <span className={styles.imageFailed}>{t.imageUnavailable}</span>;
  }
  return (
    <img
      className={`p-doc-image ${styles.image}`}
      src={kbImageUrl(source.document_id, imageId)}
      alt={source.quote}
      loading="lazy"
      decoding="async"
      onError={() => setFailed(true)}
    />
  );
}

/**
 * Карточка источника (концепция §4.3): номер, документ, место — страница или момент записи, —
 * цитата до трёх строк; у источника-картинки на месте цитаты — сама картинка.
 */
export function SourceCard({ source, active, opening, openerId, onActive, onOpen }: SourceCardProps) {
  const scope = SCOPE_LABELS[source.scope];
  return (
    <button
      type="button"
      className={active ? `${styles.card} ${styles.active}` : styles.card}
      data-opener={openerId}
      onMouseEnter={() => onActive(source.n)}
      onMouseLeave={() => onActive(null)}
      onFocus={() => onActive(source.n)}
      onBlur={() => onActive(null)}
      onClick={onOpen}
    >
      <span className={styles.head}>
        <span className={styles.number}>{source.n}</span>
        <TitleHint text={source.document_title}>
          <span className={styles.title}>{source.document_title}</span>
        </TitleHint>
        {opening && <Spinner type="mini" caption={null} />}
      </span>
      <span className={styles.meta}>
        {source.timecode_seconds !== null && (
          <span className={styles.timecode}>
            <IconMediaAudioVolumeARegular16 aria-hidden="true" />
            <span>
              {t.from} <span className="p-mono">{formatTimecode(source.timecode_seconds)}</span>
            </span>
          </span>
        )}
        {source.page !== null && <span>{t.page(source.page)}</span>}
        {scope && <span>{scope}</span>}
      </span>
      {source.image_id === null ? (
        <span className={styles.quote}>{source.quote}</span>
      ) : (
        <CardImage source={source} imageId={source.image_id} />
      )}
    </button>
  );
}
