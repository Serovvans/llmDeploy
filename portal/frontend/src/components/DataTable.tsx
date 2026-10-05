import { IconArrowADownRegular16 } from '@skbkontur/icons/IconArrowADownRegular16';
import { IconArrowAUpRegular16 } from '@skbkontur/icons/IconArrowAUpRegular16';

import type { SortOrder } from '../api/types';
import styles from './DataTable.module.css';

/** Таблица на токенах темы (концепция §4.7): шапку и строки задаёт экран. */
export function DataTable({ children }: { children: React.ReactNode }) {
  return <table className={styles.table}>{children}</table>;
}

interface SortableHeaderProps {
  /** Список отсортирован по этому столбцу; по умолчанию — да (таблица с одним сортируемым столбцом). */
  active?: boolean;
  order: SortOrder;
  onToggle: () => void;
  children: React.ReactNode;
}

/** Сортируемый заголовок столбца: кнопка в `<th>` с `aria-sort` (концепция §9). */
export function SortableHeader({ active = true, order, onToggle, children }: SortableHeaderProps) {
  const Icon = order === 'asc' ? IconArrowADownRegular16 : IconArrowAUpRegular16;
  const sort = order === 'asc' ? 'ascending' : 'descending';
  return (
    <th scope="col" aria-sort={active ? sort : 'none'}>
      <button type="button" className={styles.sort} onClick={onToggle}>
        {children}
        {active && (
          <span aria-hidden="true" className={styles.sortIcon}>
            <Icon />
          </span>
        )}
      </button>
    </th>
  );
}
