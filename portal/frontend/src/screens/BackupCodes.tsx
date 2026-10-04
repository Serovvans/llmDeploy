import { Button, Checkbox } from '@skbkontur/react-ui';
import { useEffect, useId, useState } from 'react';

import { useCopy } from '../hooks/useCopy';
import { texts } from '../texts';
import styles from './SecondFactorScreen.module.css';

interface BackupCodesProps {
  codes: string[];
  onProceed: () => void;
}

function downloadAsFile(codes: string[]): void {
  // `blob:` используется только для скачивания файла, собранного в браузере (концепция §12).
  const url = URL.createObjectURL(new Blob([`${codes.join('\n')}\n`], { type: 'text/plain;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = texts.backupCodes.fileName;
  link.click();
  URL.revokeObjectURL(url);
}

/** Резервные коды — вторая часть экрана настройки: показываются один раз (концепция §5.4). */
export function BackupCodes({ codes, onProceed }: BackupCodesProps) {
  const [saved, setSaved] = useState(false);
  const [showSaveFirst, setShowSaveFirst] = useState(false);
  const copy = useCopy();
  const errorId = useId();

  // Закрытие вкладки до отметки — стандартное предупреждение браузера о несохранённых данных.
  useEffect(() => {
    if (saved) {
      return;
    }
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [saved]);

  const proceed = () => {
    if (saved) {
      onProceed();
    } else {
      setShowSaveFirst(true);
    }
  };

  return (
    <>
      <p>{texts.backupCodes.intro}</p>
      <ul className={styles.codes}>
        {codes.map((code) => (
          <li key={code}>{code}</li>
        ))}
      </ul>
      <div className={styles.actions}>
        <Button onClick={() => void copy.copy(codes.join('\n'))}>{copy.label}</Button>
        <Button onClick={() => downloadAsFile(codes)}>{texts.backupCodes.download}</Button>
      </div>
      <p>{texts.backupCodes.advice}</p>
      <div className={styles.saved}>
        <Checkbox
          checked={saved}
          onValueChange={(checked) => {
            setSaved(checked);
            setShowSaveFirst(false);
          }}
          error={showSaveFirst && !saved}
          aria-describedby={showSaveFirst && !saved ? errorId : undefined}
        >
          {texts.backupCodes.saved}
        </Checkbox>
        {showSaveFirst && !saved && (
          <p id={errorId} className={styles.savedError} role="alert">
            {texts.backupCodes.saveFirst}
          </p>
        )}
      </div>
      <div>
        <Button use="primary" size="large" onClick={proceed}>
          {texts.backupCodes.proceed}
        </Button>
      </div>
    </>
  );
}
