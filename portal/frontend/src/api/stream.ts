/** Разбор потока событий портала (контракт §6.1): `event:` и одна строка `data:` с JSON, пустая строка — конец события. */

/** События потока ответа (контракт §6.2). Событие неизвестного типа разбор пропускает. */
export type StreamEvent =
  | { type: 'start'; user_message_id: string; assistant_message_id: string }
  | { type: 'context_truncated'; dropped_messages: number }
  | { type: 'reasoning_delta'; text: string }
  | { type: 'delta'; text: string }
  | { type: 'title'; title: string }
  | { type: 'done'; status: 'complete' | 'length_limit' }
  | { type: 'error'; code: string; message: string };

const KNOWN_EVENTS = new Set<string>([
  'start',
  'context_truncated',
  'reasoning_delta',
  'delta',
  'title',
  'done',
  'error',
]);

function toEvent(name: string, data: string): StreamEvent | null {
  if (!KNOWN_EVENTS.has(name)) {
    return null;
  }
  try {
    return { ...(JSON.parse(data) as object), type: name } as StreamEvent;
  } catch {
    // Событие с неразборчивыми данными пропускается так же, как неизвестное.
    return null;
  }
}

/**
 * Читает поток и отдаёт события по мере поступления. Строки-комментарии (`: keep-alive`)
 * ничего не дают. Событие, не закрытое пустой строкой до конца потока, не отдаётся.
 */
export async function* readEvents(body: ReadableStream<Uint8Array>): AsyncGenerator<StreamEvent> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let name = '';
  let data = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        return;
      }
      buffer += decoder.decode(value, { stream: true });
      let lineEnd = buffer.indexOf('\n');
      while (lineEnd >= 0) {
        const line = buffer.slice(0, lineEnd).replace(/\r$/, '');
        buffer = buffer.slice(lineEnd + 1);
        if (line === '') {
          const event = name ? toEvent(name, data) : null;
          name = '';
          data = '';
          if (event) {
            yield event;
          }
        } else if (line.startsWith('event:')) {
          name = line.slice(6).trim();
        } else if (line.startsWith('data:')) {
          data = line.slice(5).trimStart();
        }
        lineEnd = buffer.indexOf('\n');
      }
    }
  } finally {
    reader.releaseLock();
  }
}
