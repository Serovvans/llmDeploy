import { PageHeader } from '../components/PageHeader';
import { usePageTitle } from '../hooks/usePageTitle';
import { texts } from '../texts';
import styles from './SectionStub.module.css';

type Section = 'sql' | 'cogis' | 'documents';

/** Место раздела, экран которого появится на этапе 5: заголовок и одна строка. */
export function SectionStub({ section }: { section: Section }) {
  const title = texts.sections[section];
  usePageTitle(title);
  return (
    <>
      <PageHeader title={title} />
      <p className={styles.text}>{texts.sections.notReady}</p>
    </>
  );
}
