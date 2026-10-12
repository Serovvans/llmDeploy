import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { KbDocument } from '../api/types';
import { ADMIN, CONFIG, EMPLOYEE, fail, mockApi, ok, session } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { texts } from '../texts';

const t = texts.kb;

function doc(patch: Partial<KbDocument>): KbDocument {
  return {
    id: 'k-1',
    title: 'Договор аренды 14-А.pdf',
    scope: 'shared',
    kind: 'document',
    is_cogis: false,
    tags: [],
    author: { full_name: 'Петров Алексей Сергеевич', is_me: false },
    created_at: '2026-09-12T09:00:00Z',
    page_count: 14,
    duration_seconds: null,
    status: 'ready',
    error_code: null,
    progress: null,
    images_truncated: false,
    reprocess_error: null,
    reprocessing: false,
    can_delete: false,
    ...patch,
  };
}

const DOCS = [
  doc({}),
  doc({ id: 'k-2', title: 'Скан постановления.pdf', author: { full_name: 'Иванов Иван Иванович', is_me: true }, page_count: 20, status: 'processing', progress: { pages_done: 3, pages_total: 20, recognizing: true }, can_delete: true }),
  doc({ id: 'k-3', title: 'Регламент.docx', page_count: null, status: 'error', error_code: 'file_unreadable' }),
  doc({ id: 'k-4', title: 'Плохой скан.pdf', status: 'error', error_code: 'recognition_failed', can_delete: true, is_cogis: true }),
  doc({ id: 'k-5', title: 'Очередь.txt', page_count: null, status: 'queued', can_delete: true }),
  doc({ id: 'k-6', title: 'Огромный.pdf', status: 'error', error_code: 'document_too_long', can_delete: true }),
  doc({ id: 'k-7', title: 'Неизвестная причина.pdf', status: 'error', error_code: 'new_reason', can_delete: true }),
];

function page(items: KbDocument[], total = items.length) {
  return ok({ items, page: 1, page_size: 50, total });
}

function setup(items: KbDocument[] = DOCS, theme?: 'light' | 'dark', user = EMPLOYEE) {
  if (theme) {
    window.localStorage.setItem('portal.theme', theme);
  }
  const server = mockApi({
    'GET /api/auth/session': () => ok(session('ready', user)),
    'GET /api/kb/tags': () => ok({ items: ['аренда', 'Минстрой'] }),
    'GET /api/kb/documents': (_body, url) => {
      const scope = url.searchParams.get('scope');
      const tags = url.searchParams.getAll('tags').map((tag) => tag.toLowerCase());
      const inScope = scope === 'personal' ? items.filter((item) => item.author.is_me) : items.filter((item) => item.scope === scope);
      return page(tags.length ? inScope.filter((item) => item.tags.some((tag) => tags.includes(tag.toLowerCase()))) : inScope);
    },
  });
  renderApp('/knowledge');
  return { server, user: userEvent.setup({ applyAccept: false }) };
}

async function row(title: string): Promise<HTMLElement> {
  return (await screen.findByText(title)).closest('tr') as HTMLElement;
}

function listQueries(): URLSearchParams[] {
  return vi
    .mocked(fetch)
    .mock.calls.map(([input]) => new URL(String(input), 'http://localhost'))
    .filter((url) => url.pathname === '/api/kb/documents')
    .map((url) => url.searchParams);
}

describe('база знаний: список', () => {
  it.each(['light', 'dark'] as const)('состояния словами, ход распознавания, причины ошибок (тема %s)', async (theme) => {
    setup(DOCS, theme);
    expect(await screen.findByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe(theme);

    const ready = within(await row('Договор аренды 14-А.pdf'));
    expect(ready.getByText('Готов')).toBeInTheDocument();
    expect((await row('Договор аренды 14-А.pdf')).textContent?.replace(/\u00a0/g, ' ')).toContain('Петров А. С.');
    expect(ready.getByText('12.09.2026')).toBeInTheDocument();
    expect(ready.getByText('14 стр.')).toBeInTheDocument();
    expect(ready.queryByRole('button', { name: /Действия/ })).not.toBeInTheDocument();

    const scan = within(await row('Скан постановления.pdf'));
    expect(scan.getByText('Распознаётся')).toBeInTheDocument();
    expect(scan.getByText('стр. 3 из 20')).toBeInTheDocument();
    expect(scan.getByText('Вы')).toBeInTheDocument();

    // Чужой документ: вместо «Удалите…» — к кому обратиться; страниц нет — «—».
    const broken = within(await row('Регламент.docx'));
    expect(broken.getByText('Ошибка')).toBeInTheDocument();
    expect(broken.getByText('Файл повреждён или защищён паролем. Обратитесь к автору документа или администратору')).toBeInTheDocument();
    expect(broken.getByText('—')).toBeInTheDocument();

    const badScan = within(await row('Плохой скан.pdf'));
    expect(
      badScan.getByText('Не удалось распознать скан. Обработайте документ заново; если не поможет — замените скан на более чёткий'),
    ).toBeInTheDocument();
    expect(badScan.getByText('Документация CoGIS')).toBeInTheDocument();

    expect(within(await row('Очередь.txt')).getByText('В очереди')).toBeInTheDocument();
    expect(
      within(await row('Огромный.pdf')).getByText('Документ слишком большой для базы знаний. Удалите его и добавьте по частям'),
    ).toBeInTheDocument();
    // Неизвестная причина — как внутренний сбой.
    expect(
      within(await row('Неизвестная причина.pdf')).getByText('Не удалось обработать документ. Обработайте его заново'),
    ).toBeInTheDocument();
  });

  it('«Обработать заново» — только при причинах, где повтор может помочь', async () => {
    const { server, user } = setup();
    const items = async (title: string) => {
      await user.click(within(await row(title)).getByRole('button', { name: `Действия: ${title}` }));
      await screen.findByText(t.menu.remove);
      const labels = menuItems().map((item) => item.textContent);
      await user.keyboard('{Escape}');
      return labels;
    };
    expect(await items('Плохой скан.pdf')).toEqual(['Изменить теги', 'Обработать заново', 'Удалить']);
    expect(await items('Огромный.pdf')).toEqual(['Изменить теги', 'Удалить']);
    expect(await items('Очередь.txt')).toEqual(['Изменить теги', 'Удалить']);
    // Неизвестная причина — как внутренний сбой: повтор может помочь.
    expect(await items('Неизвестная причина.pdf')).toEqual(['Изменить теги', 'Обработать заново', 'Удалить']);

    // Нет права — сообщение, строка перезапрашивается.
    server.on('POST /api/kb/documents/k-4/retry', () => fail(403, 'forbidden'));
    await user.click(within(await row('Плохой скан.pdf')).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.retry));
    expect(await screen.findByText(t.retryForbidden)).toBeInTheDocument();

    // Успех: строка перерисовывается по ответу сервера.
    server.on('POST /api/kb/documents/k-4/retry', () => ok(doc({ id: 'k-4', title: 'Плохой скан.pdf', status: 'queued', can_delete: true })));
    await user.click(within(await row('Плохой скан.pdf')).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.retry));
    expect(await screen.findByText(t.retried)).toBeInTheDocument();
    expect(within(await row('Плохой скан.pdf')).getByText('В очереди')).toBeInTheDocument();
  });

  it('вкладка, поиск и сортировка уходят на сервер; во вкладке «Моя» нет столбца «Автор»', async () => {
    const { user } = setup();
    await row('Договор аренды 14-А.pdf');
    expect(Object.fromEntries(listQueries()[0] ?? [])).toEqual({ scope: 'shared', page: '1', page_size: '50', sort: 'created_at', order: 'desc' });
    expect(screen.getByRole('columnheader', { name: t.columns.added })).toHaveAttribute('aria-sort', 'descending');
    expect(screen.getByRole('columnheader', { name: t.columns.title })).toHaveAttribute('aria-sort', 'none');

    await user.click(screen.getByRole('button', { name: t.columns.title }));
    await waitFor(() => expect(listQueries().at(-1)?.get('sort')).toBe('title'));
    expect(listQueries().at(-1)?.get('order')).toBe('asc');
    await user.click(screen.getByRole('button', { name: t.columns.title }));
    await waitFor(() => expect(listQueries().at(-1)?.get('order')).toBe('desc'));

    await user.type(screen.getByLabelText(t.search), 'аренд');
    await waitFor(() => expect(listQueries().at(-1)?.get('q')).toBe('аренд'));

    await user.click(screen.getByText(t.tabs.personal));
    await waitFor(() => expect(listQueries().at(-1)?.get('scope')).toBe('personal'));
    await waitFor(() => expect(screen.queryByRole('columnheader', { name: t.columns.author })).not.toBeInTheDocument());
  });

  it('пустые состояния: общая, моя, поиск', async () => {
    const { user } = setup([]);
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.shared.title })).toBeInTheDocument();
    expect(screen.getByText(t.empty.shared.text)).toBeInTheDocument();

    await user.click(screen.getByText(t.tabs.personal));
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.personal.title })).toBeInTheDocument();

    await user.type(screen.getByLabelText(t.search), 'нет такого');
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.search.title })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: t.empty.search.action }));
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.personal.title })).toBeInTheDocument();
  });

  it('пока документы обрабатываются, список обновляется сам', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { server } = setup([doc({ status: 'queued' })]);
      await screen.findByText('В очереди');
      server.on('GET /api/kb/documents', () => page([doc({})]));
      await vi.advanceTimersByTimeAsync(5100);
      expect(await screen.findByText('Готов')).toBeInTheDocument();
      const calls = server.callsTo('GET /api/kb/documents').length;
      await vi.advanceTimersByTimeAsync(11000);
      expect(server.callsTo('GET /api/kb/documents')).toHaveLength(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it('пока обработка готового документа обновляется, список обновляется сам и показывает повторный сбой', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { server } = setup([doc({ reprocessing: true, can_delete: true })]);
      await screen.findByText(t.reprocessing);
      server.on('GET /api/kb/documents', () => page([doc({ reprocess_error: 'internal_error', can_delete: true })]));
      await vi.advanceTimersByTimeAsync(5100);
      expect(await screen.findByText(t.reprocessFailed.other)).toBeInTheDocument();
      expect(screen.queryByText(t.reprocessing)).not.toBeInTheDocument();
      const calls = server.callsTo('GET /api/kb/documents').length;
      await vi.advanceTimersByTimeAsync(11000);
      expect(server.callsTo('GET /api/kb/documents')).toHaveLength(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it('удаление: текст по базе, строка убирается сразу; уже удалённый — без ошибки; иной отказ — сообщение', async () => {
    const { server, user } = setup();
    const openRemove = async (title: string) => {
      await user.click(within(await row(title)).getByRole('button', { name: /Действия/ }));
      await user.click(await screen.findByText(t.menu.remove));
    };

    server.on('DELETE /api/kb/documents/k-5', () => fail(500, 'internal_error'));
    await openRemove('Очередь.txt');
    expect(await screen.findByText('Удалить документ „Очередь.txt“ из общей базы?')).toBeInTheDocument();
    expect(screen.getByText(t.remove.bodyShared)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: t.remove.action }));
    expect(await screen.findByText(t.remove.failed)).toBeInTheDocument();
    expect(screen.getByText('Очередь.txt')).toBeInTheDocument();

    server.on('DELETE /api/kb/documents/k-5', () => fail(404, 'not_found'));
    server.on('GET /api/kb/documents', () => page(DOCS.filter((item) => item.id !== 'k-5')));
    await openRemove('Очередь.txt');
    await user.click(await screen.findByRole('button', { name: t.remove.action }));
    expect(await screen.findByText(t.remove.done)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Очередь.txt')).not.toBeInTheDocument());
  });

  it('просмотр открывается только у готового документа: с первой страницы, сведения — из строки списка', async () => {
    const { server, user } = setup();
    server.on('GET /api/kb/documents/k-1/text', () =>
      ok({ page: 1, page_count: 14, recognized: false, segments: [{ text: 'Текст первой страницы', highlight: false }] }),
    );
    expect(within(await row('Скан постановления.pdf')).queryByRole('button', { name: 'Скан постановления.pdf' })).not.toBeInTheDocument();

    await user.click(within(await row('Договор аренды 14-А.pdf')).getByRole('button', { name: 'Договор аренды 14-А.pdf' }));
    expect(await screen.findByText('Текст первой страницы')).toBeInTheDocument();
    expect(screen.getByText('Страница 1 из 14')).toBeInTheDocument();
    expect(screen.getByText(/Общая база · добавил\(а\) Петров А\. С\./)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: texts.viewer.openOriginal })).toHaveAttribute('href', '/api/kb/documents/k-1/file#page=1');
    expect(server.callsTo('GET /api/kb/documents/k-1')).toHaveLength(0);
  });
});

describe('база знаний: открытие просмотра', () => {
  it('пока нет ответа сервера, в нажатой строке — индикатор; сбой — уведомление, панель не появляется', async () => {
    const { server, user } = setup();
    let respond: (() => void) | undefined;
    server.on('GET /api/kb/documents/k-1/text', () => new Promise((resolve) => (respond = () => resolve(fail(503, 'service_unavailable')))));

    const line = await row('Договор аренды 14-А.pdf');
    expect(line.querySelector('[data-tid="Spinner__root"]')).toBeNull();
    await user.click(within(line).getByRole('button', { name: 'Договор аренды 14-А.pdf' }));
    await waitFor(() => expect(line.querySelector('[data-tid="Spinner__root"]')).not.toBeNull());

    respond?.();
    expect(await screen.findByText('Не удалось открыть документ. Попробуйте ещё раз')).toBeInTheDocument();
    await waitFor(() => expect(line.querySelector('[data-tid="Spinner__root"]')).toBeNull());
    expect(screen.queryByRole('link', { name: texts.viewer.openOriginal })).not.toBeInTheDocument();
  });
});

describe('база знаний: добавление', () => {
  // В jsdom нет `DataTransfer`, а выбор файлов библиотеки им пользуется: достаточно пустой подмены.
  beforeEach(() => {
    vi.stubGlobal(
      'DataTransfer',
      class {
        items = { add: () => undefined };
        files = null;
      },
    );
  });

  function fileInput(): HTMLInputElement {
    return document.querySelector('[role="dialog"] input[type="file"]') as HTMLInputElement;
  }

  /** Выбор файлов в окне: событие изменения поля, как при выборе в диалоге браузера. */
  function pick(...files: File[]) {
    fireEvent.change(fileInput(), { target: { files } });
  }

  async function openAdd(user: ReturnType<typeof userEvent.setup>) {
    await waitFor(() => expect(screen.getAllByRole('button', { name: t.add })[0]).toBeEnabled());
    await user.click(screen.getAllByRole('button', { name: t.add })[0] as HTMLElement);
    return within(await screen.findByRole('dialog'));
  }

  it('файлы уходят по одному по кнопке «Добавить» с выбранной базой и отметкой CoGIS; окно закрывается', async () => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents', () => ok(doc({ status: 'queued' }), 201));
    const dialog = await openAdd(user);
    expect(
      dialog.getByText(
        'PDF, DOCX, TXT, MD, JPG, PNG — до 50 МБ каждый, PDF до 500 страниц. ' +
          'Аудио и видео с русской речью (MP3, WAV, M4A, OGG, FLAC, MP4, MKV, WebM, MOV) — до 2 ГБ и до 3 часов. ' +
          'Сканы распознаются, записи расшифровываются автоматически.',
      ),
    ).toBeInTheDocument();

    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    expect(dialog.getByRole('alert')).toHaveTextContent(t.upload.noFiles);

    pick(new File(['%PDF-'], 'первый.pdf'), new File(['text'], 'второй.txt'));
    expect(dialog.getByText('первый.pdf')).toBeInTheDocument();
    expect(server.callsTo('POST /api/kb/documents')).toHaveLength(0);

    await user.click(dialog.getByLabelText(t.upload.cogis));
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    expect(await screen.findByText('Добавлено документов: 2. Они появятся в ответах после обработки')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    const sent = server.callsTo('POST /api/kb/documents').map((call) => call.body as FormData);
    expect(sent.map((form) => [(form.get('file') as File).name, form.get('scope'), form.get('is_cogis')])).toEqual([
      ['первый.pdf', 'shared', 'true'],
      ['второй.txt', 'shared', 'true'],
    ]);
  });

  it('в личную базу отметка CoGIS не предлагается и не отправляется; предвыбрана база открытой вкладки', async () => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents', () => ok(doc({ scope: 'personal', status: 'queued' }), 201));
    await row('Договор аренды 14-А.pdf');
    await user.click(screen.getByText(t.tabs.personal));
    const dialog = await openAdd(user);

    expect(dialog.getByLabelText(t.upload.toPersonal)).toBeChecked();
    expect(dialog.queryByLabelText(t.upload.cogis)).not.toBeInTheDocument();
    pick(new File(['text'], 'личный.md'));
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    await waitFor(() => expect(server.callsTo('POST /api/kb/documents')).toHaveLength(1));
    const form = server.callsTo('POST /api/kb/documents')[0]?.body as FormData;
    expect([form.get('scope'), form.get('is_cogis')]).toEqual(['personal', 'false']);
  });

  it('часть файлов отклонена: окно остаётся с ошибками, «Отмена» становится «Закрыть»', async () => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents', (body) => {
      const name = ((body as FormData).get('file') as File).name;
      if (name === 'хороший.pdf') {
        return ok(doc({ status: 'queued' }), 201);
      }
      if (name === 'длинный.pdf') {
        return fail(422, 'too_many_pages', { details: { max_pages: 500 } });
      }
      if (name === 'битый.pdf') {
        return fail(422, 'file_unreadable');
      }
      throw new TypeError('Failed to fetch');
    });
    const dialog = await openAdd(user);

    const big = new File(['x'], 'огромный.pdf');
    Object.defineProperty(big, 'size', { value: 52428801 });
    pick(
      new File(['%PDF-'], 'хороший.pdf'),
      new File(['%PDF-'], 'длинный.pdf'),
      new File(['%PDF-'], 'битый.pdf'),
      new File(['%PDF-'], 'сеть.pdf'),
      new File(['x'], 'чертёж.dwg'),
      big,
    );
    // Тип и размер проверены до отправки.
    expect(dialog.getByText('Такой формат не поддерживается')).toBeInTheDocument();
    expect(dialog.getByText('Файл больше 50 МБ')).toBeInTheDocument();

    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    expect(await dialog.findByText('В файле больше 500 страниц. Разделите его на части')).toBeInTheDocument();
    expect(await dialog.findByText('Файл повреждён или защищён паролем')).toBeInTheDocument();
    expect(await dialog.findByText('Не удалось загрузить. Повторите')).toBeInTheDocument();
    expect(await screen.findByText('Добавлено документов: 1. Они появятся в ответах после обработки')).toBeInTheDocument();
    expect(dialog.getByText(t.upload.uploaded)).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: t.upload.close })).toBeInTheDocument();
    expect(server.callsTo('POST /api/kb/documents')).toHaveLength(4);

    // Повторное «Добавить» отправляет только файл, чья загрузка оборвалась.
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    await waitFor(() => expect(server.callsTo('POST /api/kb/documents')).toHaveLength(5));
    expect(((server.callsTo('POST /api/kb/documents').at(-1)?.body as FormData).get('file') as File).name).toBe('сеть.pdf');
  });

  it.each([
    [{ status: 'ready', error_code: null, can_delete: false }, 'Такой документ уже есть в этой базе: „Договор аренды 14-А“, добавил(а) Петров А. С. 12.09.2026'],
    [{ status: 'error', error_code: 'no_text', can_delete: false }, 'Такой документ уже есть в этой базе, но обработан с ошибкой: „Договор аренды 14-А“, добавил(а) Петров А. С. 12.09.2026. Обратитесь к автору документа или администратору'],
    [{ status: 'error', error_code: 'recognition_failed', can_delete: true }, 'Такой документ уже есть в этой базе, но обработан с ошибкой: „Договор аренды 14-А“. Загружать его снова не нужно — закройте это окно и выберите в списке „Обработать заново“'],
    [{ status: 'error', error_code: 'unknown_reason', can_delete: true }, 'Такой документ уже есть в этой базе, но обработан с ошибкой: „Договор аренды 14-А“. Загружать его снова не нужно — закройте это окно и выберите в списке „Обработать заново“'],
    [{ status: 'error', error_code: 'document_too_long', can_delete: true }, 'Этот файл уже добавлен и обработан с ошибкой: „Договор аренды 14-А“. Тот же файл не поможет — удалите документ в списке и добавьте исправленный'],
  ])('повтор файла %o', async (existing, text) => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents', () =>
      fail(409, 'duplicate_document', {
        details: {
          document: { id: 'k-1', title: 'Договор аренды 14-А', author_full_name: 'Петров Алексей Сергеевич', created_at: '2026-09-12T09:00:00Z', ...existing },
        },
      }),
    );
    const dialog = await openAdd(user);
    pick(new File(['%PDF-'], 'копия.pdf'));
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    const alert = await dialog.findByRole('alert');
    expect(alert.textContent).toBe(text);
  });
});

describe('база знаний: правки дизайн-ревью', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'DataTransfer',
      class {
        items = { add: () => undefined };
        files = null;
      },
    );
  });

  it('удалена последняя строка страницы 2 — переход на страницу 1, а не «пока пусто»', async () => {
    const last = doc({ id: 'k-51', title: 'Последний.pdf', can_delete: true });
    const server = mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'GET /api/kb/documents': (_body, url) =>
        url.searchParams.get('page') === '2'
          ? ok({ items: [last], page: 2, page_size: 50, total: 51 })
          : ok({ items: [doc({})], page: 1, page_size: 50, total: 51 }),
      'DELETE /api/kb/documents/k-51': () => ok(),
    });
    renderApp('/knowledge');
    const user = userEvent.setup();

    await screen.findByText('Договор аренды 14-А.pdf');
    await user.click(screen.getByText('2', { selector: '[data-tid="Paging__pageLink"]' }));
    await user.click(within(await row('Последний.pdf')).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.remove));
    server.on('GET /api/kb/documents', () => ok({ items: [doc({})], page: 1, page_size: 50, total: 50 }));
    await user.click(await screen.findByRole('button', { name: t.remove.action }));

    expect(await screen.findByText('Договор аренды 14-А.pdf')).toBeInTheDocument();
    expect(listQueries().at(-1)?.get('page')).toBe('1');
    expect(screen.queryByText(t.empty.shared.title)).not.toBeInTheDocument();
  });

  it('окно «Добавить документы» не закрывается нажатием на фон', async () => {
    const { user } = setup();
    await user.click((await screen.findAllByRole('button', { name: t.add }))[0] as HTMLElement);
    await screen.findByRole('dialog');

    const background = document.querySelector('[data-tid="modal-container"]') as HTMLElement;
    fireEvent.mouseDown(background);
    fireEvent.mouseUp(background);
    fireEvent.click(background);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('настройки не загрузились: кнопка не блокируется, в окне — заметка с «Повторить», файл уходит без проверки', async () => {
    const { server, user } = setup();
    server.on('GET /api/config', () => fail(503, 'service_unavailable'));
    server.on('POST /api/kb/documents', () => ok(doc({ status: 'queued' }), 201));
    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(1));

    await user.click((await screen.findAllByRole('button', { name: t.add }))[0] as HTMLElement);
    const dialog = within(await screen.findByRole('dialog'));
    expect(await dialog.findByText(texts.common.configFailed)).toBeInTheDocument();

    fireEvent.change(document.querySelector('[role="dialog"] input[type="file"]') as HTMLInputElement, {
      target: { files: [new File(['x'], 'любой.xyz')] },
    });
    expect(dialog.getByText('любой.xyz')).toBeInTheDocument();

    // «Повторить» запрашивает настройки сразу; заметка исчезает, выбранный файл остаётся.
    server.on('GET /api/config', () => ok(CONFIG));
    await user.click(dialog.getByRole('button', { name: texts.common.retry }));
    expect(await dialog.findByText(/PDF, DOCX, TXT, MD, JPG, PNG — до 50 МБ/)).toBeInTheDocument();
    expect(dialog.queryByText(texts.common.configFailed)).not.toBeInTheDocument();
    expect(dialog.getByText('любой.xyz')).toBeInTheDocument();

    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    await waitFor(() => expect(server.callsTo('POST /api/kb/documents')).toHaveLength(1));
  });
});

describe('база знаний: «Министерства» и теги', () => {
  const MINISTRY = doc({ id: 'm-1', title: 'Приказ Минстроя.pdf', scope: 'ministries', tags: ['Минстрой', 'аренда', 'ЛЭП', '<img src=x onerror=alert(1)>'] });

  it('сотрудник: вкладка открыта на чтение — пояснение, действий в строке нет, в окне добавления нет «Министерств»', async () => {
    const { user } = setup([MINISTRY, doc({ id: 'm-2', title: 'Сломанный.pdf', scope: 'ministries', status: 'error', error_code: 'file_unreadable' })]);
    await user.click(await screen.findByText(t.tabs.ministries));
    const line = await row('Приказ Минстроя.pdf');
    expect(screen.getByText(t.ministriesNote.employee)).toBeInTheDocument();
    expect(within(line).queryByRole('button', { name: /Действия/ })).not.toBeInTheDocument();
    expect(within(line).queryByText(t.cogis)).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: t.columns.author })).toBeInTheDocument();
    expect(within(await row('Сломанный.pdf')).getByText('Файл повреждён или защищён паролем. Обратитесь к администратору')).toBeInTheDocument();

    await user.click(screen.getAllByRole('button', { name: t.add })[0] as HTMLElement);
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.queryByLabelText(t.upload.toMinistries)).not.toBeInTheDocument();
    expect(dialog.getByLabelText(t.upload.toShared)).toBeChecked();
  });

  it('пустые «Министерства»: у сотрудника действия нет, у администратора — «Добавить документы» с предвыбранной базой', async () => {
    const employee = setup([]);
    await employee.user.click(await screen.findByText(t.tabs.ministries));
    expect(await screen.findByText(t.empty.ministries.employee)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: t.add })).toHaveLength(1);
    document.body.innerHTML = '';

    const admin = setup([], undefined, ADMIN);
    await admin.user.click(await screen.findByText(t.tabs.ministries));
    expect(await screen.findByText(t.empty.ministries.admin)).toBeInTheDocument();
    expect(screen.getByText(t.ministriesNote.admin)).toBeInTheDocument();
    await admin.user.click(screen.getAllByRole('button', { name: t.add })[1] as HTMLElement);
    expect(within(await screen.findByRole('dialog')).getByLabelText(t.upload.toMinistries)).toBeChecked();
  });

  it('теги в строке — данные и кнопки отбора; отбор уходит на сервер, сохраняется при смене вкладки и сбрасывается целиком', async () => {
    const { user } = setup([MINISTRY, doc({ tags: ['аренда'] }), doc({ id: 'k-9', title: 'Без тегов.pdf' })]);
    await user.click(await screen.findByText(t.tabs.ministries));
    const line = await row('Приказ Минстроя.pdf');
    // Видны первые теги, остальные — за «+2»; враждебное значение остаётся текстом в имени кнопки.
    expect(within(line).getByRole('button', { name: 'Ещё теги: ЛЭП, <img src=x onerror=alert(1)>' })).toHaveTextContent('+2');
    expect(line.querySelector('img')).toBeNull();

    await user.click(within(line).getByRole('button', { name: 'Показать документы с тегом аренда' }));
    await waitFor(() => expect(listQueries().at(-1)?.getAll('tags')).toEqual(['аренда']));
    expect(listQueries().at(-1)?.get('page')).toBe('1');
    // Повторное нажатие тег не дублирует.
    await user.click(within(await row('Приказ Минстроя.pdf')).getByRole('button', { name: 'Показать документы с тегом аренда' }));

    await user.click(screen.getByText(t.tabs.shared));
    await row('Договор аренды 14-А.pdf');
    expect(listQueries().at(-1)?.getAll('tags')).toEqual(['аренда']);
    expect(screen.queryByText('Без тегов.pdf')).not.toBeInTheDocument();

    await user.click(screen.getByText(t.tabs.personal));
    expect(await screen.findByText(t.empty.tags.text)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: t.empty.tags.action }));
    expect(await screen.findByRole('heading', { level: 2, name: t.empty.personal.title })).toBeInTheDocument();
    expect(listQueries().at(-1)?.getAll('tags')).toEqual([]);
  });

  it('отбор: тегов не больше предела из настроек', async () => {
    const { server, user } = setup([doc({ tags: ['аренда', 'ЛЭП'] })]);
    server.on('GET /api/config', () => ok({ ...CONFIG, kb: { ...CONFIG.kb, tags: { ...CONFIG.kb.tags, max_selected: 1 } } }));
    const line = await row('Договор аренды 14-А.pdf');
    await waitFor(() => expect(server.callsTo('GET /api/config').length).toBeGreaterThan(0));
    await user.click(within(line).getByRole('button', { name: 'Показать документы с тегом аренда' }));
    expect(await screen.findByText('Можно выбрать не больше 1 тега')).toBeInTheDocument();
    await user.click(within(await row('Договор аренды 14-А.pdf')).getByRole('button', { name: 'Показать документы с тегом ЛЭП' }));
    expect(listQueries().at(-1)?.getAll('tags')).toEqual(['аренда']);
  });

  async function openTags(user: ReturnType<typeof userEvent.setup>, title: string) {
    await user.click(within(await row(title)).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.tags));
    return within(await screen.findByRole('dialog'));
  }

  it('правка тегов: список уходит целиком, повтор в другом регистре и лишний тег не добавляются, строка обновляется', async () => {
    const mine = doc({ id: 'k-8', title: 'Мой.pdf', tags: ['аренда'], can_delete: true });
    const { server, user } = setup([mine]);
    server.on('GET /api/config', () => ok({ ...CONFIG, kb: { ...CONFIG.kb, tags: { ...CONFIG.kb.tags, max_per_document: 2, max_length: 6 } } }));
    server.on('PUT /api/kb/documents/k-8/tags', (body) => ok({ ...mine, tags: (body as { tags: string[] }).tags }));
    await waitFor(() => expect(server.callsTo('GET /api/config').length).toBeGreaterThan(0));
    const dialog = await openTags(user, 'Мой.pdf');
    const field = dialog.getByLabelText(t.tags.label);

    await user.type(field, 'Аренда,');
    await user.type(field, '  ЛЭП ,');
    await user.type(field, 'слишком');
    // Слишком длинный тег остаётся в поле, сохранять рано.
    expect(await dialog.findByText('Тег длиннее 6 символов. Сократите его')).toBeInTheDocument();
    expect(dialog.getByRole('button', { name: t.tags.save })).toBeDisabled();
    await user.type(field, '{Backspace>7/}');
    await waitFor(() => expect(dialog.getByRole('button', { name: t.tags.save })).toBeEnabled());
    await user.type(field, 'ещё,');
    expect(await dialog.findByText('У документа может быть не больше 2 тегов')).toBeInTheDocument();

    await user.click(dialog.getByRole('button', { name: t.tags.save }));
    expect(await screen.findByText(t.tags.saved)).toBeInTheDocument();
    expect(server.callsTo('PUT /api/kb/documents/k-8/tags')[0]?.body).toEqual({ tags: ['аренда', 'ЛЭП'] });
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(within(await row('Мой.pdf')).getByRole('button', { name: 'Показать документы с тегом ЛЭП' })).toBeInTheDocument();
  });

  it('правка тегов: отказ сервера по полю — под полем; нет права — окно закрывается; документ удалён — строка убирается', async () => {
    const mine = doc({ id: 'k-8', title: 'Мой.pdf', can_delete: true });
    const { server, user } = setup([mine, doc({ id: 'm-1', title: 'Приказ.pdf', scope: 'ministries', can_delete: true })], undefined, ADMIN);
    server.on('PUT /api/kb/documents/k-8/tags', () =>
      fail(422, 'validation_error', { fields: [{ field: 'tags', code: 'invalid_format', message: 'Недопустимый тег.' }] }),
    );
    // Документ без тегов открывает то же окно ссылкой «Добавить теги».
    await user.click(within(await row('Мой.pdf')).getByRole('button', { name: t.tags.add }));
    let dialog = within(await screen.findByRole('dialog'));
    await user.click(dialog.getByRole('button', { name: t.tags.save }));
    expect(await dialog.findByText(t.tags.invalid)).toBeInTheDocument();

    server.on('PUT /api/kb/documents/k-8/tags', () => fail(503, 'service_unavailable'));
    await user.click(dialog.getByRole('button', { name: t.tags.save }));
    expect(await dialog.findByText(t.tags.failed)).toBeInTheDocument();

    server.on('PUT /api/kb/documents/k-8/tags', () => fail(404, 'not_found'));
    await user.click(dialog.getByRole('button', { name: t.tags.save }));
    expect(await screen.findByText(texts.viewer.deleted)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Мой.pdf')).not.toBeInTheDocument());

    await user.click(screen.getByText(t.tabs.ministries));
    server.on('PUT /api/kb/documents/m-1/tags', () => fail(403, 'forbidden'));
    dialog = await openTags(user, 'Приказ.pdf');
    await user.click(dialog.getByRole('button', { name: t.tags.save }));
    expect(await screen.findByText(t.tags.forbiddenMinistries)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

describe('база знаний: записи и обновление обработки', () => {
  const RECORDING = doc({ id: 'r-1', title: 'Совещание 03.10.mp3', scope: 'shared', kind: 'recording', page_count: null, can_delete: true });

  it('состояния записи: ход расшифровки, длительность, причины ошибок; повтор — только где он поможет', async () => {
    const { user } = setup([
      { ...RECORDING, status: 'processing', duration_seconds: 4320, progress: { seconds_done: 1500, seconds_total: 4320 } },
      { ...RECORDING, id: 'r-2', title: 'Без хода.mp3', status: 'processing' },
      { ...RECORDING, id: 'r-3', title: 'Длинная.mp4', status: 'error', error_code: 'media_too_long' },
      { ...RECORDING, id: 'r-4', title: 'Тишина.wav', status: 'error', error_code: 'no_text' },
      { ...RECORDING, id: 'r-5', title: 'Сбой.mp3', status: 'error', error_code: 'transcription_failed' },
      { ...RECORDING, id: 'r-6', title: 'Истёк.mp3', status: 'error', error_code: 'source_expired' },
    ]);
    const transcribing = within(await row('Совещание 03.10.mp3'));
    expect(transcribing.getByText('Расшифровывается')).toBeInTheDocument();
    expect(transcribing.getByText('25 мин из 1 ч 12 мин')).toBeInTheDocument();
    expect(transcribing.getByText('1 ч 12 мин')).toBeInTheDocument();
    expect((await row('Без хода.mp3')).textContent).not.toMatch(/ из /);
    expect(within(await row('Длинная.mp4')).getByText('Запись длиннее 3 часов. Удалите её и добавьте по частям')).toBeInTheDocument();
    expect(within(await row('Тишина.wav')).getByText('В записи не нашлось русской речи. Удалите документ')).toBeInTheDocument();

    const menu = async (title: string) => {
      await user.click(within(await row(title)).getByRole('button', { name: /Действия/ }));
      await screen.findByText(t.menu.remove);
      const labels = menuItems().map((item) => item.textContent);
      await user.keyboard('{Escape}');
      return labels;
    };
    expect(await menu('Сбой.mp3')).toContain(t.menu.retry);
    expect(await menu('Истёк.mp3')).not.toContain(t.menu.retry);
    expect(await menu('Длинная.mp4')).not.toContain(t.menu.retry);
  });

  it('исходный файл записи удалён, пока список был открыт: без сообщения, причина — в строке, повтора больше нет', async () => {
    const { server, user } = setup([{ ...RECORDING, status: 'error', error_code: 'transcription_failed' }]);
    server.on('POST /api/kb/documents/r-1/retry', () => fail(409, 'source_unavailable'));
    await user.click(within(await row('Совещание 03.10.mp3')).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.retry));
    expect(
      await screen.findByText('Не удалось расшифровать запись, а исходный файл уже удалён. Удалите документ и добавьте запись снова'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Сообщение сервера/)).not.toBeInTheDocument();
    await user.click(within(await row('Совещание 03.10.mp3')).getByRole('button', { name: /Действия/ }));
    await screen.findByText(t.menu.remove);
    expect(menuItems().map((item) => item.textContent)).toEqual([t.menu.tags, t.menu.remove]);
  });

  it('обновление обработки не удалось: отметка и «Обработать заново» — только у того, кто вправе; документ остаётся «Готов»', async () => {
    const failed = doc({ id: 'k-8', title: 'Мой.pdf', reprocess_error: 'recognition_failed', can_delete: true });
    const { server, user } = setup([
      failed,
      doc({ id: 'k-9', title: 'Чужой.pdf', reprocess_error: 'internal_error' }),
      doc({ id: 'k-10', title: 'Много картинок.pdf', images_truncated: true }),
    ]);
    const mine = await row('Мой.pdf');
    expect(within(mine).getByText('Готов')).toBeInTheDocument();
    expect(within(mine).getByText(t.reprocessFailed.recognition_failed)).toBeInTheDocument();
    // Название нажимается: документ работает в прежнем виде.
    expect(within(mine).getByRole('button', { name: 'Мой.pdf' })).toBeInTheDocument();
    expect((await row('Чужой.pdf')).textContent).not.toContain('Обновить обработку не удалось');
    expect(within(await row('Много картинок.pdf')).getByText(t.imagesTruncated)).toBeInTheDocument();

    // `retry` снимает отметку сразу: строка — по ответу сервера, остаётся «Готов», пункта больше нет.
    server.on('POST /api/kb/documents/k-8/retry', () => ok({ ...failed, reprocess_error: null, reprocessing: true }));
    await user.click(within(mine).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.retry));
    expect(await screen.findByText(t.reprocessRetried)).toBeInTheDocument();
    const updated = await row('Мой.pdf');
    expect(within(updated).getByText('Готов')).toBeInTheDocument();
    expect(within(updated).getByText(t.reprocessing)).toBeInTheDocument();
    await user.click(within(updated).getByRole('button', { name: /Действия/ }));
    await screen.findByText(t.menu.remove);
    expect(menuItems().map((item) => item.textContent)).toEqual([t.menu.tags, t.menu.remove]);
  });
});

describe('база знаний: добавление записей', () => {
  beforeEach(() => {
    vi.stubGlobal(
      'DataTransfer',
      class {
        items = { add: () => undefined };
        files = null;
      },
    );
  });

  function sized(name: string, size: number): File {
    const file = new File(['x'], name);
    Object.defineProperty(file, 'size', { value: size });
    return file;
  }

  function pick(...files: File[]) {
    fireEvent.change(document.querySelector('[role="dialog"] input[type="file"]') as HTMLInputElement, { target: { files } });
  }

  async function openAdd(user: ReturnType<typeof userEvent.setup>) {
    await waitFor(() => expect(screen.getAllByRole('button', { name: t.add })[0]).toBeEnabled());
    await user.click(screen.getAllByRole('button', { name: t.add })[0] as HTMLElement);
    return within(await screen.findByRole('dialog'));
  }

  const MB = 1024 * 1024;

  it('запись уходит своим маршрутом в личную базу с тегами; записи — строго по одной, документ их не ждёт', async () => {
    const { server, user } = setup();
    const responses: (() => void)[] = [];
    server.on('POST /api/kb/documents/media', () => new Promise((resolve) => responses.push(() => resolve(ok(doc({ kind: 'recording', status: 'queued' }), 201)))));
    server.on('POST /api/kb/documents', () => ok(doc({ status: 'queued' }), 201));
    const dialog = await openAdd(user);

    await user.click(dialog.getByLabelText(t.upload.cogis));
    pick(sized('совещание.mp4', 1228 * MB), sized('планёрка.mp3', 10 * MB), new File(['%PDF-'], 'договор.pdf'));
    // Место переставлено на личную базу с заметкой; отметка CoGIS вместе с общей базой пропала.
    expect(dialog.getByLabelText(t.upload.toPersonal)).toBeChecked();
    expect(dialog.getByText(t.upload.recordingNotice)).toBeInTheDocument();
    expect(dialog.queryByLabelText(t.upload.cogis)).not.toBeInTheDocument();

    await user.type(dialog.getByLabelText(t.tags.label), 'Совещания,');
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));

    await waitFor(() => expect(server.uploads).toHaveLength(1));
    expect(server.uploads[0]?.path).toBe(
      `/api/kb/documents/media?${new URLSearchParams({ file_name: 'совещание.mp4', scope: 'personal', tags: 'Совещания' }).toString()}`,
    );
    expect(server.callsTo('POST /api/kb/documents/media')[0]?.headers['X-Portal-Csrf']).toBe('1');
    expect(dialog.getByText(t.upload.waiting)).toBeInTheDocument();
    await waitFor(() => expect(server.callsTo('POST /api/kb/documents')).toHaveLength(1));
    const form = server.callsTo('POST /api/kb/documents')[0]?.body as FormData;
    expect([form.get('scope'), form.get('is_cogis'), form.getAll('tags')]).toEqual(['personal', 'false', ['Совещания']]);

    act(() => server.uploads[0]?.progress(28, 100));
    expect(await dialog.findByText('343 МБ из 1,1 ГБ · 28 %')).toBeInTheDocument();
    expect(dialog.getByRole('progressbar', { name: 'Загрузка файла совещание.mp4' })).toHaveAttribute('aria-valuenow', '28');

    responses[0]?.();
    await waitFor(() => expect(server.uploads).toHaveLength(2));
    responses[1]?.();
    expect(await screen.findByText('Добавлено документов: 3. Они появятся в ответах после обработки')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('место, выбранное вручную, запись не переставляет; отметка CoGIS к записям не относится', async () => {
    const { user } = setup();
    const dialog = await openAdd(user);
    await user.click(dialog.getByLabelText(t.upload.toPersonal));
    await user.click(dialog.getByLabelText(t.upload.toShared));
    pick(sized('совещание.mp3', MB));
    expect(dialog.getByLabelText(t.upload.toShared)).toBeChecked();
    expect(dialog.getByText(t.upload.recordingNotice)).toBeInTheDocument();
    // Одни записи — отметки нет; с документом — есть, с оговоркой.
    expect(dialog.queryByLabelText(t.upload.cogis)).not.toBeInTheDocument();
    pick(new File(['%PDF-'], 'договор.pdf'));
    expect(dialog.getByLabelText(t.upload.cogis)).toBeInTheDocument();
    expect(dialog.getByText(t.upload.cogisNotForRecordings)).toBeInTheDocument();
  });

  it('закрытие вкладки во время загрузки любого файла — предупреждение браузера; после загрузки его нет', async () => {
    const { server, user } = setup();
    let finish: () => void = () => undefined;
    server.on('POST /api/kb/documents', () => new Promise((resolve) => (finish = () => resolve(ok(doc({ status: 'queued' }), 201)))));
    const leave = () => {
      const event = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    const dialog = await openAdd(user);
    pick(new File(['%PDF-'], 'первый.pdf'));
    expect(leave()).toBe(false);

    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    await waitFor(() => expect(server.callsTo('POST /api/kb/documents')).toHaveLength(1));
    expect(leave()).toBe(true);

    finish();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(leave()).toBe(false);
  });

  it('отмена загрузки и выход из очереди; закрытие окна во время загрузки переспрашивает и прерывает', async () => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents/media', () => new Promise(() => undefined));
    const dialog = await openAdd(user);
    pick(sized('первая.mp3', MB), sized('вторая.mp3', MB), sized('третья.mp3', MB));
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    await waitFor(() => expect(server.uploads).toHaveLength(1));

    // Ждущая запись убирается из очереди; отменённая освобождает место следующей.
    await user.click(dialog.getAllByRole('button', { name: t.upload.removeWaiting })[0] as HTMLElement);
    expect(dialog.queryByText('вторая.mp3')).not.toBeInTheDocument();
    await user.click(dialog.getByRole('button', { name: t.upload.cancel }));
    expect(server.uploads[0]?.aborted).toBe(true);
    await waitFor(() => expect(server.uploads).toHaveLength(2));
    expect(server.uploads[1]?.path).toContain(encodeURIComponent('третья.mp3'));

    await user.click(dialog.getByRole('button', { name: texts.common.cancel }));
    expect(await screen.findByText(t.upload.abort.title)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: t.upload.abort.stay })).toHaveFocus());
    await user.click(screen.getByRole('button', { name: t.upload.abort.stay }));
    expect(dialog.getByText('третья.mp3')).toBeInTheDocument();
    expect(server.uploads[1]?.aborted).toBe(false);

    await user.click(dialog.getByRole('button', { name: texts.common.cancel }));
    await user.click(await screen.findByRole('button', { name: t.upload.abort.action }));
    expect(server.uploads[1]?.aborted).toBe(true);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });

  it('окно исчезло без «Прервать» (переход на другой экран): незагруженные запись и документ прерваны', async () => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents/media', () => new Promise(() => undefined));
    server.on('POST /api/kb/documents', () => new Promise(() => undefined));
    const dialog = await openAdd(user);
    pick(sized('совещание.mp3', MB), new File(['%PDF-'], 'договор.pdf'));
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    await waitFor(() => expect(server.uploads).toHaveLength(1));
    await waitFor(() => expect(server.callsTo('POST /api/kb/documents')).toHaveLength(1));
    const documentSignal = vi
      .mocked(fetch)
      .mock.calls.find(([input, init]) => String(input) === '/api/kb/documents' && init?.method === 'POST')?.[1]?.signal;
    expect(documentSignal?.aborted).toBe(false);

    fireEvent.click(screen.getByRole('link', { name: texts.nav.chat, hidden: true }));
    await waitFor(() => expect(screen.getByTestId('path')).toHaveTextContent('/chat'));
    expect(server.uploads[0]?.aborted).toBe(true);
    expect(documentSignal?.aborted).toBe(true);
  });

  it.each([
    [() => fail(429, 'too_many_uploads'), 'Сейчас загружается слишком много записей. Повторите через несколько минут', true],
    [() => fail(408, 'upload_timeout', { message: 'Загрузка прервалась: данные перестали поступать. Повторите загрузку' }), 'Загрузка прервалась: данные перестали поступать. Повторите загрузку', true],
    [() => fail(507, 'media_quota_exceeded', { details: { reason: 'user' } }), t.upload.quotaUser, false],
    [() => fail(507, 'media_quota_exceeded', { details: { reason: 'free_space' } }), 'На портале закончилось место для записей. Сообщите администратору', false],
    [() => fail(415, 'unsupported_file_type'), t.upload.mediaInvalid, false],
    [() => fail(413, 'file_too_large', { details: { max_bytes: 2147483648 } }), 'Файл больше 2 ГБ', false],
    [() => fail(403, 'forbidden'), t.upload.ministriesForbidden, true],
  ])('отказ в приёме записи %#: текст у файла; сам интерфейс не повторяет', async (respond, text, retryable) => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents/media', respond);
    const dialog = await openAdd(user);
    pick(sized('совещание.mp3', MB));
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    expect(await dialog.findByText(text)).toBeInTheDocument();
    expect(server.uploads).toHaveLength(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    if (retryable) {
      await user.click(dialog.getByRole('button', { name: texts.common.retry }));
      await waitFor(() => expect(server.uploads).toHaveLength(2));
    } else {
      expect(dialog.queryByRole('button', { name: texts.common.retry })).not.toBeInTheDocument();
    }
  });

  it('проверка до отправки: запись больше предела; при выключенном приёме запись не поддерживается', async () => {
    const { server, user } = setup();
    let dialog = await openAdd(user);
    pick(sized('огромная.mkv', 2147483649));
    expect(dialog.getByText('Файл больше 2 ГБ')).toBeInTheDocument();
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    expect(server.uploads).toHaveLength(0);
    await user.click(dialog.getByRole('button', { name: texts.common.cancel }));
    document.body.innerHTML = '';

    const off = setup();
    off.server.on('GET /api/config', () => ok({ ...CONFIG, media: { ...CONFIG.media, enabled: false } }));
    await waitFor(() => expect(off.server.callsTo('GET /api/config').length).toBeGreaterThan(0));
    dialog = await openAdd(off.user);
    expect(dialog.getByText('PDF, DOCX, TXT, MD, JPG, PNG — до 50 МБ каждый, PDF до 500 страниц. Сканы распознаются автоматически.')).toBeInTheDocument();
    pick(sized('совещание.mp3', MB));
    expect(dialog.getByText(t.upload.unsupported)).toBeInTheDocument();
    expect(dialog.queryByText(t.upload.recordingNotice)).not.toBeInTheDocument();
  });

  it('повтор файла при заданных тегах: подсказка, где их сменить, — тому, кто вправе', async () => {
    const { server, user } = setup();
    server.on('POST /api/kb/documents', () =>
      fail(409, 'duplicate_document', {
        details: {
          document: { id: 'k-1', title: 'Договор аренды 14-А', author_full_name: 'Петров Алексей Сергеевич', created_at: '2026-09-12T09:00:00Z', status: 'ready', error_code: null, can_delete: true },
        },
      }),
    );
    const dialog = await openAdd(user);
    pick(new File(['%PDF-'], 'копия.pdf'));
    await user.type(dialog.getByLabelText(t.tags.label), 'аренда,');
    await user.click(dialog.getByRole('button', { name: t.upload.submit }));
    expect((await dialog.findByText(/Такой документ уже есть/)).textContent?.replace(/\u00a0/g, ' ')).toBe(
      'Такой документ уже есть в этой базе: „Договор аренды 14-А“, добавил(а) Петров А. С. 12.09.2026. Чтобы сменить его теги, выберите в списке „Изменить теги“',
    );
  });
});
