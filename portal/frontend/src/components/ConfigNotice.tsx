import { useSession } from '../session/SessionContext';
import { texts } from '../texts';
import { Notice } from './Notice';

/** Заметка на месте того, что без настроек портала показать нечего (концепция §6); исчезает, когда они загрузились. */
export function ConfigNotice() {
  const { config, configFailed, reloadConfig } = useSession();
  if (config || !configFailed) {
    return null;
  }
  return (
    <Notice kind="error" action={{ label: texts.common.retry, onClick: reloadConfig }}>
      {texts.common.configFailed}
    </Notice>
  );
}
