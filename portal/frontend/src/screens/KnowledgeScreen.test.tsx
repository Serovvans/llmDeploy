import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { KbDocument } from '../api/types';
import { fail, mockApi, ok, session } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { texts } from '../texts';

const t = texts.kb;

function doc(patch: Partial<KbDocument>): KbDocument {
  return {
    id: 'k-1',
    title: 'Договор аренды 14-А.pdf',
    scope: 'shared',
    is_cogis: false,
    author: { full_name: 'Петров Алексей Сергеевич', is_me: false },
    created_at: '2026-09-12T09:00:00Z',
    page_count: 14,
    status: 'ready',
    error_code: null,
    progress: null,
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

function setup(items: KbDocument[] = DOCS, theme?: 'light' | 'dark') {
  if (theme) {
    window.localStorage.setItem('portal.theme', theme);
  }
  const server = mockApi({
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/kb/documents': (_body, url) =>
      url.searchParams.get('scope') === 'shared' ? page(items) : page(items.filter((item) => item.author.is_me)),
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
    expect(ready.getByText('14')).toBeInTheDocument();
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
    expect(await items('Плохой скан.pdf')).toEqual(['Обработать заново', 'Удалить']);
    expect(await items('Огромный.pdf')).toEqual(['Удалить']);
    expect(await items('Очередь.txt')).toEqual(['Удалить']);

    server.on('POST /api/kb/documents/k-4/retry', () => ok(doc({ id: 'k-4', status: 'queued' })));
    await user.click(within(await row('Плохой скан.pdf')).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.retry));
    expect(await screen.findByText(t.retried)).toBeInTheDocument();

    // Уже в очереди — без сообщения; нет права — сообщение.
    server.on('POST /api/kb/documents/k-4/retry', () => fail(403, 'forbidden'));
    await user.click(within(await row('Плохой скан.pdf')).getByRole('button', { name: /Действия/ }));
    await user.click(await screen.findByText(t.menu.retry));
    expect(await screen.findByText(t.retryForbidden)).toBeInTheDocument();
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
    expect(dialog.getByText('PDF, DOCX, TXT, MD, JPG, PNG — до 50 МБ каждый, PDF до 500 страниц. Сканы распознаются автоматически.')).toBeInTheDocument();

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
