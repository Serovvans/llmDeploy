import { texts } from '../../texts';
import { TitleHint } from '../TitleHint';
import styles from './TagChips.module.css';

const t = texts.kb.filter;
/** Сколько тегов видно в ячейке; остальные — за «+N» (концепция §5.6: «показываются первые»). */
const VISIBLE = 2;

interface TagChipsProps {
  tags: string[];
  /** Нажатие на тег добавляет его в отбор над таблицей. */
  onPick: (tag: string) => void;
}

/** Теги документа в строке списка (свой `TagChip`, концепция §5.6): значение тега — данные, одной строкой. */
export function TagChips({ tags, onPick }: TagChipsProps) {
  const hidden = tags.slice(VISIBLE);
  return (
    <span className={styles.chips}>
      {tags.slice(0, VISIBLE).map((tag) => (
        <TitleHint key={tag} text={tag}>
          <button type="button" className={styles.chip} aria-label={t.show(tag)} onClick={() => onPick(tag)}>
            <span className={styles.label}>{tag}</span>
          </button>
        </TitleHint>
      ))}
      {hidden.length > 0 && (
        <TitleHint text={hidden.join(', ')} always>
          <button type="button" className={styles.chip} aria-label={t.more(hidden)}>
            <span className={styles.label}>+{hidden.length}</span>
          </button>
        </TitleHint>
      )}
    </span>
  );
}
