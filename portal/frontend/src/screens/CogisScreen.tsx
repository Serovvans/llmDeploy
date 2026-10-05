import { Switcher } from '@skbkontur/react-ui';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api } from '../api/client';
import type { CogisAction } from '../api/types';
import { DialogScreen } from '../components/chat/DialogScreen';
import { Notice } from '../components/Notice';
import { texts } from '../texts';
import styles from './ToolScreen.module.css';

const t = texts.cogis;
const ACTIONS = (Object.keys(t.actions) as CogisAction[]).map((value) => ({ value, label: t.actions[value] }));

/** Помощник CoGIS (концепция §5.8): как SQL-помощник, с источниками из документации CoGIS. */
export function CogisScreen() {
  const navigate = useNavigate();
  const [action, setAction] = useState<CogisAction>('write');
  const [documentationMissing, setDocumentationMissing] = useState(false);

  // Признак запрашивается при каждом открытии раздела; сбой запроса — предупреждения нет.
  useEffect(() => {
    let cancelled = false;
    api.getCogisDocumentation().then(
      ({ available }) => {
        if (!cancelled) {
          setDocumentationMissing(!available);
        }
      },
      () => {
        // Без признака раздел работает как обычно.
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <DialogScreen
      kind="cogis"
      basePath="/cogis"
      sectionTitle={t.title}
      empty={t.empty}
      params={{ action }}
      placeholder={t.placeholders[action]}
      mono={action !== 'write'}
      note={t.note}
      nothingFoundText={t.nothingFound}
      toolbar={
        <div className={styles.toolbar}>
          <Switcher items={ACTIONS} value={action} onValueChange={(value) => setAction(value as CogisAction)} />
        </div>
      }
      banner={
        documentationMissing ? (
          <Notice
            kind="warning"
            action={{
              label: t.addDocumentation,
              onClick: () => navigate('/knowledge', { state: { addCogisDocumentation: true } }),
            }}
          >
            {t.noDocumentation}
          </Notice>
        ) : undefined
      }
    />
  );
}
