import { SingleToast } from '@skbkontur/react-ui';
import { useCallback, useEffect, useRef, useState } from 'react';

import { texts } from '../texts';

const COPIED_LABEL_MS = 2000;

/** Копирование в буфер обмена: подпись на 2 секунды меняется на «Скопировано» (концепция §8.3). */
export function useCopy(): { label: string; copy: (text: string) => Promise<void> } {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      SingleToast.push(texts.common.copyFailed, { use: 'error' });
      return;
    }
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), COPIED_LABEL_MS);
  }, []);

  return { label: copied ? texts.common.copied : texts.common.copy, copy };
}
