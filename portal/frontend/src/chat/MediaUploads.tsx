import { createContext, useContext, useEffect, useState } from 'react';

import { UploadQueue } from './uploadQueue';

const MediaUploadsContext = createContext<UploadQueue | null>(null);

/**
 * Очередь загрузки записей — одна на вкладку: сервер принимает от сотрудника одну запись одновременно
 * (контракт §1.6), поэтому записи чата и окна «Добавить документы» ждут друг друга, а не получают отказ.
 */
export function MediaUploadsProvider({ children }: { children: React.ReactNode }) {
  const [queue] = useState(() => new UploadQueue());
  useEffect(() => () => queue.clear(), [queue]);
  return <MediaUploadsContext.Provider value={queue}>{children}</MediaUploadsContext.Provider>;
}

/** Ключи записей в очереди общие: каждый пользователь очереди даёт своим записям свой префикс. */
export function useMediaUploads(): UploadQueue {
  const queue = useContext(MediaUploadsContext);
  if (!queue) {
    throw new Error('useMediaUploads вызван вне MediaUploadsProvider');
  }
  return queue;
}
