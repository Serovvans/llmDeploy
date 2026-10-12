import { kbImageUrl } from '../../api/client';
import type { Source } from '../../api/types';
import { texts } from '../../texts';
import styles from './SourceFigure.module.css';

const t = texts.chat.sources;

interface SourceFigureProps {
  source: Source & { image_id: string };
  openerId: string;
  onActive: (n: number | null) => void;
  onOpen: () => void;
  /** Картинка не загрузилась (документ удалён, сбой): из ряда она убирается. */
  onFailed: () => void;
}

/**
 * Картинка из документа под ответом (концепция §4.2): кнопка, открывает просмотр источника, как его карточка.
 * Адрес строится по источнику; рамка задана заранее, чтобы лента не прыгала при загрузке.
 */
export function SourceFigure({ source, openerId, onActive, onOpen, onFailed }: SourceFigureProps) {
  return (
    <figure className={styles.figure}>
      <button
        type="button"
        className={`p-doc-image ${styles.frame}`}
        data-opener={openerId}
        aria-label={t.figureLabel(source.document_title, source.n)}
        onMouseEnter={() => onActive(source.n)}
        onMouseLeave={() => onActive(null)}
        onFocus={() => onActive(source.n)}
        onBlur={() => onActive(null)}
        onClick={onOpen}
      >
        <img
          className={styles.image}
          src={kbImageUrl(source.document_id, source.image_id)}
          alt={source.quote}
          loading="lazy"
          decoding="async"
          onError={onFailed}
        />
      </button>
      <figcaption className={styles.caption}>
        <span className="p-mono">[{source.n}]</span> {t.figureCaption(source.document_title, source.page)}
      </figcaption>
    </figure>
  );
}
