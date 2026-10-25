import { Link, ScrollContainer } from '@skbkontur/react-ui';
import { useNavigate } from 'react-router-dom';

import { texts } from '../texts';
import styles from './Code.module.css';
import { ProjectList } from './ProjectPanel';

const w = texts.workshop;

/** Ссылка-цепочка «Мастерская кода CoGIS ›» перед названием экрана (§5.13.2). */
export function Crumb() {
  const navigate = useNavigate();
  return (
    <>
      <span className={styles.crumb}>
        <Link
          href="/code"
          onClick={(event) => {
            event.preventDefault();
            navigate('/code');
          }}
        >
          {w.title}
        </Link>
      </span>
      <span className={styles.crumbSep} aria-hidden="true">
        ›
      </span>
    </>
  );
}

interface SectionLayoutProps {
  /** Без названия шапки нет: служебное состояние со своим заголовком или загрузка. */
  title?: string;
  /** Есть ли перед названием ссылка на начало раздела (мастер). */
  crumb?: boolean;
  /** Своя рабочая область вместо прокручиваемой колонки (прежний диалог). */
  bare?: boolean;
  actions?: React.ReactNode;
  children: React.ReactNode;
}

/** Экраны раздела со списком проектов слева: начало, мастер, прежний диалог помощника CoGIS. */
export function SectionLayout({ title, crumb = false, bare = false, actions, children }: SectionLayoutProps) {
  return (
    <div className={styles.section}>
      <ProjectList />
      <div className={styles.area}>
        {title !== undefined && (
          <header className={styles.header}>
            <div className={styles.titleRow}>
              {crumb && <Crumb />}
              <h1 className={styles.title}>{title}</h1>
            </div>
            {actions}
          </header>
        )}
        {bare ? (
          children
        ) : (
          <div className={styles.scroll}>
            <ScrollContainer>{children}</ScrollContainer>
          </div>
        )}
      </div>
    </div>
  );
}
