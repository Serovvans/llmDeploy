/**
 * Очередь загрузки записей: строго по одной. На одного сотрудника сервер принимает одну запись
 * одновременно (контракт §1.6), поэтому следующая начинается после ответа на предыдущую.
 */
export class UploadQueue {
  private waiting: { key: string; run: (signal: AbortSignal) => Promise<void> }[] = [];
  private active: { key: string; controller: AbortController } | null = null;

  /** `run` сам разбирает свои отказы и не отклоняется: отказ одной записи очередь не останавливает. */
  enqueue(key: string, run: (signal: AbortSignal) => Promise<void>): void {
    this.waiting.push({ key, run });
    this.pump();
  }

  /** Убирает запись из очереди; идущую загрузку отменяет. */
  cancel(key: string): void {
    this.waiting = this.waiting.filter((job) => job.key !== key);
    if (this.active?.key === key) {
      this.active.controller.abort();
    }
  }

  /** Отменяет всё: приложение закрывается. */
  clear(): void {
    this.waiting = [];
    this.active?.controller.abort();
  }

  private pump(): void {
    const job = this.active ? undefined : this.waiting.shift();
    if (!job) {
      return;
    }
    const controller = new AbortController();
    this.active = { key: job.key, controller };
    void job.run(controller.signal).finally(() => {
      this.active = null;
      this.pump();
    });
  }
}
