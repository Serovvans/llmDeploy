import { describe, expect, it } from 'vitest';

import { readEvents, type StreamEvent } from './stream';

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  });
}

async function collect(stream: ReadableStream<Uint8Array>): Promise<StreamEvent[]> {
  const events: StreamEvent[] = [];
  for await (const event of readEvents(stream)) {
    events.push(event);
  }
  return events;
}

describe('разбор потока событий', () => {
  it('отдаёт события по порядку', async () => {
    const events = await collect(
      streamOf(
        'event: start\ndata: {"user_message_id":"u","assistant_message_id":"a"}\n\n',
        'event: reasoning_delta\ndata: {"text":"Думаю"}\n\n',
        'event: delta\ndata: {"text":"Срок аренды"}\n\nevent: done\ndata: {"status":"complete"}\n\n',
      ),
    );
    expect(events).toEqual([
      { type: 'start', user_message_id: 'u', assistant_message_id: 'a' },
      { type: 'reasoning_delta', text: 'Думаю' },
      { type: 'delta', text: 'Срок аренды' },
      { type: 'done', status: 'complete' },
    ]);
  });

  it('собирает событие, разорванное между частями, в том числе посреди русской буквы', async () => {
    const bytes = new TextEncoder().encode('event: delta\ndata: {"text":"аренда"}\n\n');
    const cut = bytes.length - 12;
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(bytes.slice(0, 5));
        controller.enqueue(bytes.slice(5, cut));
        controller.enqueue(bytes.slice(cut));
        controller.close();
      },
    });
    expect(await collect(stream)).toEqual([{ type: 'delta', text: 'аренда' }]);
  });

  it('пропускает строки keep-alive, события неизвестного типа и неразборчивые данные', async () => {
    const events = await collect(
      streamOf(
        ': keep-alive\n\n',
        'event: search_started\ndata: {}\n\n',
        'event: delta\ndata: не JSON\n\n',
        ': keep-alive\n',
        'event: delta\r\ndata: {"text":"ок"}\r\n\r\n',
      ),
    );
    expect(events).toEqual([{ type: 'delta', text: 'ок' }]);
  });

  it('событие, не закрытое пустой строкой до конца потока, не отдаётся', async () => {
    expect(await collect(streamOf('event: delta\ndata: {"text":"обрыв"}\n'))).toEqual([]);
  });
});
