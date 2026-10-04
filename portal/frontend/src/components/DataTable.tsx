import { IconArrowADownRegular16 } from '@skbkontur/icons/IconArrowADownRegular16';
import { IconArrowAUpRegular16 } from '@skbkontur/icons/IconArrowAUpRegular16';

import type { SortOrder } from '../api/types';
import styles from './DataTable.module.css';

/** Таблица на токенах темы (концепция §4.7): шапку и строки задаёт экран. */
export function DataTable({ children }: { children: React.ReactNode }) {
  return <table className={styles.table}>{children}</table>;
}

interface SortableHeaderProps {
  order: SortOrder;
  onToggle: () => void;
  children: React.ReactNode;
}

/** Сортируемый заголовок столбца: кнопка в `<th>` с `aria-sort` (концепция §9). */
export function SortableHeader({ order, onToggle, children }: SortableHeaderProps) {
  const Icon = order === 'asc' ? IconArrowADownRegular16 : IconArrowAUpRegular16;
  return (
    <th scope="col" aria-sort={order === 'asc' ? 'ascending' : 'descending'}>
      <button type="button" className={styles.sort} onClick={onToggle}>
        {children}
        <span aria-hidden="true" className={styles.sortIcon}>
          <Icon />
        </span>
      </button>
    </th>
  );
}
