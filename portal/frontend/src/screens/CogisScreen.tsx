import { SingleToast, Switcher } from '@skbkontur/react-ui';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';

import { api, isApiError } from '../api/client';
import type { CogisAction } from '../api/types';
import { useDialogs } from '../chat/ChatProvider';
import { DialogScreen } from '../components/chat/DialogScreen';
import { Notice } from '../components/Notice';
import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import styles from './ToolScreen.module.css';

const t = texts.cogis;
const ACTIONS = (Object.keys(t.actions) as CogisAction[]).map((value) => ({ value, label: t.actions[value] }));

/** Помощник CoGIS (концепция §5.8): как SQL-помощник, с источниками из документации CoGIS. */
export function CogisScreen() {
  const navigate = useNavigate();
  const [action, setAction] = useState<CogisAction>('write');
  const [documentationMissing, setDocumentationMissing] = useState(false);
  const { reloadConfig } = useSession();
  const { regenerate } = useDialogs('cogis');

  // Раздел «Код» включили, пока экран открыт: прежний диалог только для чтения (§5.13.15). С новыми
  // настройками маршрут `/cogis/:id` сам откроет диалог на `/code/dialogs/:id` (§5.13.14).
  const readOnly = (error: unknown) => {
    if (!isApiError(error, 'dialog_read_only')) {
      return false;
    }
    SingleToast.push(texts.workshop.old.readOnly);
    reloadConfig();
    return true;
  };

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
      onSendRefused={readOnly}
      onRegenerate={(id) => regenerate(id, { onRefused: readOnly })}
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
