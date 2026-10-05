import { Spinner } from '@skbkontur/react-ui';

import type { Source } from '../../api/types';
import { texts } from '../../texts';
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

/** Карточка источника (концепция §4.3): номер, документ, страница, цитата до трёх строк. */
export function SourceCard({ source, active, opening, openerId, onActive, onOpen }: SourceCardProps) {
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
        {[source.page === null ? null : t.page(source.page), source.scope === 'personal' ? t.mine : null]
          .filter(Boolean)
          .join(' · ')}
      </span>
      <span className={styles.quote}>{source.quote}</span>
    </button>
  );
}
