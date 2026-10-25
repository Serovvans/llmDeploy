import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Dialog, DocparseResult, Message, SqlRunBlock } from '../api/types';
import { CONFIG, fail, liveSse, mockApi, ok, session, sse } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { texts } from '../texts';

const ID = 'd-1';
const START = ['start', { user_message_id: 'u-1', assistant_message_id: 'a-1' }] as [string, object];

function dialog(kind: Dialog['kind'], patch: Partial<Dialog> = {}): Dialog {
  const now = new Date().toISOString();
  return { id: ID, kind, title: 'Участки с истёкшей арендой', created_at: now, updated_at: now, ...patch };
}

function message(patch: Partial<Message>): Message {
  return {
    id: 'a-1',
    role: 'assistant',
    content: '',
    status: 'complete',
    error_code: null,
    reasoning: null,
    reasoning_seconds: null,
    attachments: [],
    sources: null,
    sources_found: null,
    sql_check: null,
    sql_run: null,
    sql_dangers: null,
    dropped_messages: 0,
    memory_mode: 'full',
    created_at: '2026-10-05T09:01:00Z',
    ...patch,
  };
}

const QUESTION = message({ id: 'u-1', role: 'user', content: 'DELETE FROM parcels', created_at: '2026-10-05T09:00:00Z' });

function setup(kind: 'sql' | 'cogis', path: string, messages: Message[] = [], extra: Parameters<typeof mockApi>[0] = {}) {
  const server = mockApi({
    'GET /api/auth/session': () => ok(session('ready')),
    'GET /api/dialogs': (_body, url) =>
      ok({ items: url.searchParams.get('kind') === kind && messages.length ? [dialog(kind)] : [], next_cursor: null }),
    [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [...messages].reverse(), next_cursor: null }),
    ...extra,
  });
  renderApp(path);
  return { server, user: userEvent.setup() };
}

function field(placeholder: string): HTMLElement {
  return screen.getByPlaceholderText(placeholder);
}

function path(): string {
  return screen.getByTestId('path').textContent ?? '';
}

describe('SQL-помощник', () => {
  const t = texts.sql;

  it('действие, диалект и схема уходят с вопросом; подсказка и шрифт поля зависят от действия', async () => {
    const { server, user } = setup('sql', '/sql', [], {
      'GET /api/sql/schemas': () => ok({ items: [{ id: 's-1', name: 'Кадастр', updated_at: '2026-10-01T09:00:00Z' }] }),
      'POST /api/dialogs': () => ok(dialog('sql', { title: null }), 201),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['delta', { text: 'Готово' }], ['done', { status: 'complete' }]]),
    });

    expect(await screen.findByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    expect(await screen.findByText(t.note.withRun)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.dialogs.sql.newLabel })).toBeInTheDocument();
    // Вложений, режима ответа и базы знаний в инструменте нет.
    expect(screen.queryByRole('button', { name: texts.chat.composer.attach })).not.toBeInTheDocument();
    expect(screen.queryByText(/База знаний:/)).not.toBeInTheDocument();
    expect(field(t.placeholders.write)).not.toHaveClass('p-mono');

    await user.click(screen.getByRole('switch', { name: t.actions.optimize }));
    const input = field(t.placeholders.optimize);
    expect(input.closest('.p-mono') ?? input).toHaveClass('p-mono');

    await user.click(await screen.findByText('PostgreSQL + PostGIS'));
    await user.click(await screen.findByText('Microsoft SQL Server'));
    await user.click(screen.getByText(t.noSchema));
    await user.click(await screen.findByText('Кадастр'));
    expect(window.localStorage.getItem('portal.sql.dialect')).toBe('mssql');
    expect(window.localStorage.getItem('portal.sql.schema')).toBe('s-1');

    await user.type(input, 'SELECT * FROM parcels{Enter}');
    await waitFor(() => expect(path()).toBe(`/sql/${ID}`));
    expect(server.callsTo('POST /api/dialogs')[0]?.body).toEqual({ kind: 'sql' });
    await waitFor(() =>
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toEqual({
        content: 'SELECT * FROM parcels',
        action: 'optimize',
        dialect: 'mssql',
        schema_id: 's-1',
        // Схема выбрана, но диалект не проверяется: проверка на тестовых данных не запрашивается.
        test_run: false,
      }),
    );
    expect(await screen.findByRole('link', { name: texts.dialogs.sql.newLabel })).toBeInTheDocument();
  });

  it('запомненные диалект и схема, которых больше нет, заменяются на умолчание и «Без схемы»', async () => {
    window.localStorage.setItem('portal.sql.dialect', 'oracle');
    window.localStorage.setItem('portal.sql.schema', 's-gone');
    const { server, user } = setup('sql', `/sql/${ID}`, [], {
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['done', { status: 'complete' }]]),
      [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')),
    });

    expect(await screen.findByText('PostgreSQL + PostGIS')).toBeInTheDocument();
    expect(screen.getByText(t.noSchema)).toBeInTheDocument();
    await user.type(field(t.placeholders.write), 'Участки больше гектара{Enter}');
    await waitFor(() =>
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({
        action: 'write',
        dialect: 'postgres',
        schema_id: null,
      }),
    );
  });

  it('опасная операция в вопросе — по событию sql_question_check; под ответом — итог проверки каждого блока sql', async () => {
    const { server, user } = setup('sql', `/sql/${ID}`, [], { [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')) });
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    await user.click(screen.getByRole('switch', { name: t.actions.explain }));

    await user.type(field(t.placeholders.explain), 'DELETE FROM parcels{Enter}');
    live.push(START, ['sql_question_check', { dangers: ['delete_without_where'] }]);
    expect(await screen.findByText(t.dangers.delete_without_where)).toBeInTheDocument();

    const answer = [
      'Первый запрос:',
      '```sql',
      'SELECT cad_number',
      'FROM parcels',
      '```',
      'Второй — с ошибкой:',
      '```SQL',
      'SELECT *',
      'FORM parcels',
      'WHERE area > 1',
      '```',
      'Не sql:',
      '```python',
      'print(1)',
      '```',
      'Третий:',
      '```sql',
      'DROP TABLE parcels',
      '```',
    ].join('\n');
    live.push(['delta', { text: answer }]);
    await screen.findByText('Первый запрос:');
    // До события sql_check под блоками ничего нет.
    expect(screen.queryByText(t.check.valid)).not.toBeInTheDocument();

    live.push(
      [
        'sql_check',
        {
          blocks: [
            { index: 0, line: 2, valid: true, error: null, dangers: [] },
            { index: 1, line: 7, valid: false, error: { line: 2, column: 1, near: 'FORM parcels' }, dangers: ['update_without_where'] },
            { index: 2, line: 17, valid: true, error: null, dangers: ['drop'] },
          ],
        },
      ],
      ['done', { status: 'complete' }],
    );
    live.close();

    await waitFor(() => expect(screen.getAllByText(t.check.valid)).toHaveLength(2));
    expect(
      screen.getByText(
        (_text, node) =>
          node?.tagName === 'SPAN' &&
          node.textContent === 'Проверка нашла возможную ошибку: строка 2, позиция 1, рядом с „FORM parcels“. Проверьте запрос перед запуском',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('FORM parcels', { selector: 'span.p-mono' })).toBeInTheDocument();
    expect(screen.getByText(t.dangers.update_without_where)).toBeInTheDocument();
    expect(screen.getByText(t.dangers.drop)).toBeInTheDocument();
    // Строка с ошибкой отмечена в блоке кода — только во втором блоке sql.
    const marks = screen.getAllByTestId('sql-error-line');
    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveAttribute('data-line', '2');
    expect(marks[0]?.closest('pre')).toHaveTextContent('FORM parcels');
    // Предупреждение под вопросом осталось.
    expect(screen.getByText(t.dangers.delete_without_where)).toBeInTheDocument();
  });

  it('после перезагрузки: sql_dangers вопроса и sql_check ответа; пустой near — без «рядом с»; нет записи — ничего', async () => {
    setup('sql', `/sql/${ID}`, [
      { ...QUESTION, sql_dangers: ['truncate', 'drop'] },
      message({
        content: '```sql\nSELEC 1\n```\n\n```sql\nSELECT 2\n```',
        sql_check: { blocks: [{ index: 0, line: 1, valid: false, error: { line: 1, column: 7, near: '' }, dangers: [] }] },
      }),
    ]);

    expect(await screen.findByText(t.dangers.truncate)).toBeInTheDocument();
    expect(screen.getByText(t.dangers.drop)).toBeInTheDocument();
    expect(
      screen.getByText('Проверка нашла возможную ошибку: строка 1, позиция 7. Проверьте запрос перед запуском'),
    ).toBeInTheDocument();
    expect(screen.queryByText(t.check.valid)).not.toBeInTheDocument();
    expect(screen.getAllByTestId('sql-error-line')).toHaveLength(1);
  });

  it('итог привязан к блоку по строке открытия, а не по счёту: блок в пункте списка сервер блоком не считает', async () => {
    const content = [
      '10. Сначала очистите журнал:',
      '',
      '    ```sql',
      '    DELETE FROM log WHERE ts < now();',
      '    ```',
      '',
      '11. Затем:',
      '',
      '```sql',
      'DROP TABLE users;',
      '```',
    ].join('\n');
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({ content, sql_check: { blocks: [{ index: 0, line: 9, valid: true, error: null, dangers: ['drop'] }] } }),
    ]);

    const warning = await screen.findByText(t.dangers.drop);
    const blocks = [...document.querySelectorAll('pre')];
    expect(blocks).toHaveLength(2);
    // Предупреждение стоит под DROP TABLE, а под DELETE … WHERE нет ничего.
    expect(blocks[1]).toHaveTextContent('DROP TABLE users;');
    expect(blocks[1]?.compareDocumentPosition(warning)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.getAllByText(t.check.valid)).toHaveLength(1);
    expect(screen.queryByText(t.check.orphan, { exact: false })).not.toBeInTheDocument();
  });

  it('строки считаются как на сервере — только по \\n: одиночный \\r и \\r\\n номер не сдвигают', async () => {
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({
        content: 'Первая\rвсё ещё первая\r\n\n```sql\r\nSELECT 1\r\n```',
        sql_check: { blocks: [{ index: 0, line: 3, valid: true, error: null, dangers: [] }] },
      }),
    ]);

    expect(await screen.findByText(t.check.valid)).toBeInTheDocument();
  });

  it('запись без блока на её строке: предупреждение под текстом ответа, без строки и позиции; безобидная запись не видна', async () => {
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({
        // Сервер видит блок после <details>, разбор Markdown в интерфейсе — нет.
        content: '<details>\n```sql\nDROP TABLE t\n```\n</details>\n\nТекст',
        sql_check: {
          blocks: [
            { index: 0, line: 40, valid: false, error: { line: 1, column: 6, near: 'TABLE t' }, dangers: ['drop'] },
            { index: 1, line: 41, valid: false, error: { line: 1, column: 1, near: '' }, dangers: [] },
            { index: 2, line: 42, valid: true, error: null, dangers: [] },
            { index: 3, line: 43, valid: null, unchecked: 'too_large', error: null, dangers: [] },
            // Запись старого сообщения без `line` — тоже под текстом ответа.
            { index: 4, valid: true, error: null, dangers: ['truncate'] },
          ],
        },
      }),
    ]);

    const notices = await screen.findAllByText(t.check.orphan, { exact: false });
    expect(notices).toHaveLength(3);
    expect(notices[2]).toHaveTextContent(`${t.check.orphan}. ${t.dangers.truncate}`);
    expect(notices[0]).toHaveTextContent(
      `${t.check.orphan}. ${t.dangers.drop}. Проверка нашла возможную ошибку рядом с „TABLE t“. Проверьте запрос перед запуском`,
    );
    expect(notices[1]).toHaveTextContent(
      `${t.check.orphan}. Проверка нашла возможную ошибку в запросе. Проверьте его перед запуском`,
    );
    expect(screen.queryByText(t.check.valid)).not.toBeInTheDocument();
    expect(screen.queryByText(t.check.unchecked.too_large as string)).not.toBeInTheDocument();
    expect(screen.queryByTestId('sql-error-line')).not.toBeInTheDocument();
  });

  it('диалог другого раздела по адресу этого — «не найден», чужая лента не показывается', async () => {
    setup('sql', `/sql/${ID}`, [], {
      [`GET /api/dialogs/${ID}`]: () => ok(dialog('chat')),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [message({ content: 'Ответ из чата' }), QUESTION], next_cursor: null }),
    });

    expect(await screen.findByText(texts.dialogs.sql.notFound)).toBeInTheDocument();
    expect(screen.queryByText('Ответ из чата')).not.toBeInTheDocument();
  });

  it.each([
    ['too_large', t.check.unchecked.too_large],
    ['too_complex', t.check.unchecked.too_complex],
    ['new_reason', t.check.uncheckedOther],
    [null, t.check.uncheckedOther],
  ])('синтаксис не проверен (%s): строка без «галочки», опасные операции показаны', async (unchecked, text) => {
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({
        content: '```sql\nDROP TABLE parcels\n```',
        sql_check: { blocks: [{ index: 0, line: 1, valid: null, unchecked, error: null, dangers: ['drop'] }] },
      }),
    ]);

    const line = await screen.findByText(text as string);
    expect(line.querySelector('svg')).toBeNull();
    expect(screen.getByText(t.dangers.drop)).toBeInTheDocument();
    expect(screen.queryByText(t.check.valid)).not.toBeInTheDocument();
    expect(screen.queryByTestId('sql-error-line')).not.toBeInTheDocument();
  });

  it.each([
    ['sql', texts.sql.placeholders.write],
    ['cogis', texts.cogis.placeholders.write],
  ] as const)('слова раздела %s: «уже формируется ответ», заметка о длинном диалоге и её действие', async (kind, placeholder) => {
    const words = texts.dialogs[kind];
    const { user } = setup(kind, `/${kind}/${ID}`, [QUESTION, message({ content: 'Ответ', dropped_messages: 2 })], {
      [`POST /api/dialogs/${ID}/messages`]: () => fail(409, 'generation_in_progress'),
    });

    expect(await screen.findByText(words.longDialog)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: words.newLabel })).toHaveLength(2);
    await user.type(field(placeholder), 'Ещё вопрос{Enter}');
    expect(await screen.findByText(words.inProgress)).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: words.newLabel }).at(-1) as HTMLElement);
    await waitFor(() => expect(path()).toBe(`/${kind}`));
  });

  it('«Ответить заново» при удалённой схеме: список обновляется, запрос повторяется один раз с текущим выбором', async () => {
    window.localStorage.setItem('portal.sql.schema', 's-2');
    const { server, user } = setup('sql', `/sql/${ID}`, [QUESTION, message({ content: 'Прежний ответ' })], {
      'GET /api/sql/schemas': () => ok({ items: [{ id: 's-2', name: 'ЕГРН', updated_at: '2026-10-01T09:00:00Z' }] }),
    });
    const bodies: unknown[] = [];
    server.on(`POST /api/dialogs/${ID}/regenerate`, (body) => {
      bodies.push(body);
      return bodies.length === 1
        ? fail(422, 'schema_not_found')
        : sse([START, ['delta', { text: 'Новый ответ' }], ['done', { status: 'complete' }]]);
    });

    await user.click(await screen.findByRole('button', { name: texts.chat.regenerate }));
    expect(await screen.findByText(t.schemaReplaced)).toBeInTheDocument();
    expect(await screen.findByText('Новый ответ')).toBeInTheDocument();
    expect(bodies).toEqual([undefined, { schema_id: 's-2' }]);

    // Повторный отказ — текст под панелью запроса, без третьей попытки.
    server.on(`POST /api/dialogs/${ID}/regenerate`, () => fail(422, 'schema_not_found'));
    await user.click(screen.getByRole('button', { name: texts.chat.regenerate }));
    expect(await screen.findByText(t.schemaDeleted)).toBeInTheDocument();
    expect(screen.getByText('Новый ответ')).toBeInTheDocument();
  });

  describe('«Мои схемы»', () => {
    const s = t.schemas;
    const SCHEMAS = [{ id: 's-1', name: 'Кадастр', updated_at: '2026-10-01T09:00:00Z' }];

    /** Открытая панель со списком из одной схемы; `current` — что сервер отдаёт в списке. */
    async function openPanel(extra: Parameters<typeof mockApi>[0] = {}) {
      const current = { items: SCHEMAS };
      const opened = setup('sql', '/sql', [], { 'GET /api/sql/schemas': () => ok({ items: current.items }), ...extra });
      await opened.user.click(await screen.findByRole('button', { name: t.mySchemas }));
      expect(await screen.findByText(s.privacy)).toBeInTheDocument();
      expect(await screen.findByText('Кадастр', { selector: 'span' })).toBeInTheDocument();
      return { ...opened, current };
    }

    it('создание: ошибки пустой и слишком длинной формы проверяет интерфейс', async () => {
      const { server, user } = await openPanel();
      await user.click(screen.getByRole('button', { name: s.add }));
      await user.click(screen.getByRole('button', { name: s.save }));
      expect(screen.getByText(s.enterName)).toBeInTheDocument();
      expect(screen.getByText(s.enterContent)).toBeInTheDocument();
      expect(screen.getByText(s.contentHint)).toBeInTheDocument();

      // Длинные значения ставятся одним событием: посимвольный набор сотни знаков здесь ничего не проверяет.
      fireEvent.change(screen.getByLabelText(s.name), { target: { value: 'я'.repeat(101) } });
      fireEvent.change(screen.getByLabelText(s.content), { target: { value: 'x'.repeat(50001) } });
      await user.click(screen.getByRole('button', { name: s.save }));
      expect(screen.getByText(s.nameTooLong)).toBeInTheDocument();
      expect(screen.getByText('Описание слишком длинное — сократите до 50 000 символов')).toBeInTheDocument();
      expect(server.callsTo('POST /api/sql/schemas')).toHaveLength(0);
    });

    it('создание: отказы сервера — у поля и над формой; сохранённая схема сразу выбрана', async () => {
      const { server, user, current } = await openPanel();
      await user.click(screen.getByRole('button', { name: s.add }));
      fireEvent.change(screen.getByLabelText(s.name), { target: { value: 'ЕГРН' } });
      fireEvent.change(screen.getByLabelText(s.content), { target: { value: 'CREATE TABLE parcels (id int);' } });

      server.on('POST /api/sql/schemas', () => fail(409, 'schema_name_taken'));
      await user.click(screen.getByRole('button', { name: s.save }));
      expect(await screen.findByText(s.nameTaken)).toBeInTheDocument();
      server.on('POST /api/sql/schemas', () => fail(409, 'schema_limit_reached'));
      await user.click(screen.getByRole('button', { name: s.save }));
      expect(await screen.findByText('Сохранено 50 схем — больше нельзя. Удалите ненужную, чтобы добавить новую')).toBeInTheDocument();

      server.on('POST /api/sql/schemas', (body) => {
        current.items = [...SCHEMAS, { id: 's-2', name: 'ЕГРН', updated_at: '2026-10-05T09:00:00Z' }];
        return ok({ id: 's-2', updated_at: '2026-10-05T09:00:00Z', ...(body as object) }, 201);
      });
      await user.click(screen.getByRole('button', { name: s.save }));
      expect(await screen.findByText(s.saved)).toBeInTheDocument();
      // Новая схема сразу выбрана в «Схема базы».
      await waitFor(() => expect(window.localStorage.getItem('portal.sql.schema')).toBe('s-2'));
    });

    it('правка запрашивает схему целиком, сохранение отправляет оба поля', async () => {
      const { server, user } = await openPanel({
        'GET /api/sql/schemas/s-1': () => ok({ id: 's-1', name: 'Кадастр', content: 'CREATE TABLE a (id int);', updated_at: '' }),
        'PUT /api/sql/schemas/s-1': (body) => ok({ id: 's-1', updated_at: '', ...(body as object) }),
      });
      await user.click(await screen.findByRole('button', { name: 'Действия: Кадастр' }));
      await user.click(menuItems().find((item) => item.textContent === s.edit) as HTMLElement);
      await waitFor(() => expect(screen.getByLabelText(s.content)).toHaveValue('CREATE TABLE a (id int);'));
      await user.type(screen.getByLabelText(s.name), ' 2');
      await user.click(screen.getByRole('button', { name: s.save }));
      await waitFor(() =>
        expect(server.callsTo('PUT /api/sql/schemas/s-1')[0]?.body).toEqual({ name: 'Кадастр 2', content: 'CREATE TABLE a (id int);' }),
      );
    });

    it('удаление: подтверждение; схема, удалённая в другой вкладке, — своё уведомление', async () => {
      const { user } = await openPanel({ 'DELETE /api/sql/schemas/s-1': () => fail(404, 'not_found') });
      await user.click(await screen.findByRole('button', { name: 'Действия: Кадастр' }));
      await user.click(menuItems().find((item) => item.textContent === s.remove) as HTMLElement);
      expect(await screen.findByText('Удалить схему „Кадастр“?')).toBeInTheDocument();
      await user.click(screen.getByRole('button', { name: s.remove }));
      expect(await screen.findByText(s.alreadyRemoved)).toBeInTheDocument();
    });
  });
});

describe('SQL-помощник: правки дизайн-ревью', () => {
  const t = texts.sql;
  const s = t.schemas;
  const SCHEMAS = [{ id: 's-1', name: 'Кадастр', updated_at: '2026-10-01T09:00:00Z' }];

  it('настройки не загрузились: вместо диалекта — заметка, вопрос не уходит; после «Повторить» — уходит', async () => {
    const { server, user } = setup('sql', '/sql', [], {
      'GET /api/config': () => fail(503, 'service_unavailable'),
      'POST /api/dialogs': () => ok(dialog('sql', { title: null }), 201),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['delta', { text: 'Готово' }], ['done', { status: 'complete' }]]),
    });

    expect(await screen.findByText(texts.common.configFailed)).toBeInTheDocument();
    expect(screen.queryByLabelText(t.dialect)).not.toBeInTheDocument();
    // Без настроек нет ни переключателя проверки, ни строки о том, что портал делает с запросами.
    expect(screen.queryByRole('switch', { name: t.testRun.label })).not.toBeInTheDocument();
    expect(screen.queryByText(t.note.withRun)).not.toBeInTheDocument();
    expect(screen.queryByText(t.note.withoutRun)).not.toBeInTheDocument();

    await user.type(field(t.placeholders.write), 'Участки больше гектара');
    await user.click(screen.getByRole('button', { name: texts.chat.composer.send }));
    // Тот же текст — под панелью запроса; нажатие сразу повторяет загрузку настроек.
    expect(screen.getAllByText(texts.common.configFailed)).toHaveLength(2);
    expect(server.callsTo('POST /api/dialogs')).toHaveLength(0);
    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(2));

    server.on('GET /api/config', () => ok(CONFIG));
    await user.click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByLabelText(t.dialect)).toBeInTheDocument();
    expect(field(t.placeholders.write)).toHaveValue('Участки больше гектара');
    // Настройки загрузились: текст об отказе под панелью запроса исчез вместе с заметкой.
    await waitFor(() => expect(screen.queryByText(texts.common.configFailed)).not.toBeInTheDocument());

    await user.click(screen.getByRole('button', { name: texts.chat.composer.send }));
    expect(await screen.findByText('Готово')).toBeInTheDocument();
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({ dialect: 'postgres' });
  });

  it('первый запрос настроек ещё идёт: «Отправить» ждёт, текста об ошибке и повторного запроса нет', async () => {
    let answer: (response: ReturnType<typeof ok>) => void = () => undefined;
    const { server, user } = setup('sql', '/sql', [], {
      'GET /api/config': () => new Promise((resolve) => (answer = resolve)),
      'POST /api/dialogs': () => ok(dialog('sql', { title: null }), 201),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['delta', { text: 'Готово' }], ['done', { status: 'complete' }]]),
    });

    await user.type(await screen.findByPlaceholderText(t.placeholders.write), 'Участки больше гектара');
    await user.keyboard('{Enter}');
    expect(screen.queryByText(texts.common.configFailed)).not.toBeInTheDocument();
    // Кнопка в состоянии `loading`: подпись скрыта индикатором, нажать нельзя.
    expect(screen.getByText(texts.chat.composer.send).closest('button')).toBeDisabled();
    expect(server.callsTo('GET /api/config')).toHaveLength(1);
    expect(server.callsTo('POST /api/dialogs')).toHaveLength(0);
    expect(field(t.placeholders.write)).toHaveValue('Участки больше гектара');

    answer(ok(CONFIG));
    expect(await screen.findByText('PostgreSQL + PostGIS')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: texts.chat.composer.send }));
    expect(await screen.findByText('Готово')).toBeInTheDocument();
  });

  it('«Мои схемы» без настроек: панель открывается, схема сохраняется без проверки длины в браузере', async () => {
    const { server, user } = setup('sql', '/sql', [], { 'GET /api/config': () => fail(503, 'service_unavailable') });
    server.on('POST /api/sql/schemas', () => fail(409, 'schema_limit_reached'));

    await user.click(await screen.findByRole('button', { name: t.mySchemas }));
    await user.click(await screen.findByRole('button', { name: s.add }));
    await user.type(screen.getByLabelText(s.name), 'ЕГРН');
    fireEvent.change(screen.getByLabelText(s.content), { target: { value: 'x'.repeat(50001) } });
    await user.click(screen.getByRole('button', { name: s.save }));
    expect(await screen.findByText('Сообщение сервера: schema_limit_reached.')).toBeInTheDocument();
    expect(server.callsTo('POST /api/sql/schemas')).toHaveLength(1);
  });

  it('«Мои схемы»: нажатие мимо панели её не закрывает и набранное не теряет', async () => {
    const { user } = setup('sql', '/sql', [], { 'GET /api/sql/schemas': () => ok({ items: SCHEMAS }) });
    await user.click(await screen.findByRole('button', { name: t.mySchemas }));
    await user.click(await screen.findByRole('button', { name: s.add }));
    await user.type(screen.getByLabelText(s.name), 'ЕГРН');

    await user.click(screen.getByRole('heading', { level: 1, name: t.title }));
    expect(screen.getByLabelText(s.name)).toHaveValue('ЕГРН');
  });

  it('«Мои схемы»: фокус возвращается после формы и после окна удаления; ошибка — фокус в поле', async () => {
    let schemas = SCHEMAS;
    const { server, user } = setup('sql', '/sql', [], { 'GET /api/sql/schemas': () => ok({ items: schemas }) });
    await user.click(await screen.findByRole('button', { name: t.mySchemas }));

    // Форма: ошибка только у описания — фокус в нём; «Отмена» возвращает фокус на «Добавить схему».
    await user.click(await screen.findByRole('button', { name: s.add }));
    await user.type(screen.getByLabelText(s.name), 'ЕГРН');
    await user.click(screen.getByRole('button', { name: s.save }));
    expect(screen.getByLabelText(s.content)).toHaveFocus();
    await user.click(screen.getByRole('button', { name: texts.common.cancel }));
    await waitFor(() => expect(screen.getByRole('button', { name: s.add })).toHaveFocus());

    // Окно удаления: «Отмена» возвращает фокус на меню строки.
    const openRemove = async () => {
      await user.click(await screen.findByRole('button', { name: 'Действия: Кадастр' }));
      await user.click(menuItems().find((item) => item.textContent === s.remove) as HTMLElement);
      await screen.findByText('Удалить схему „Кадастр“?');
    };
    await openRemove();
    await user.click(screen.getByRole('button', { name: texts.common.cancel }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Действия: Кадастр' })).toHaveFocus());

    // Схема удалена, строки больше нет: фокус — на «Добавить схему».
    server.on('DELETE /api/sql/schemas/s-1', () => {
      schemas = [];
      return ok();
    });
    await openRemove();
    await user.click(screen.getByRole('button', { name: s.remove }));
    expect(await screen.findByText(s.empty)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: s.add })).toHaveFocus();
  });
});

function runBlock(patch: Partial<SqlRunBlock>): SqlRunBlock {
  return {
    index: 0,
    line: 1,
    status: 'passed',
    reason: null,
    fixture: null,
    result: null,
    affected_rows: null,
    error: null,
    fixed_sql: null,
    ...patch,
  };
}

describe('SQL-помощник: переименование', () => {
  const t = texts.sql;

  it('на время правки названия настройки остаются в заголовке', async () => {
    const { user } = setup('sql', `/sql/${ID}`, [QUESTION], { 'GET /api/sql/schemas': () => ok({ items: [] }) });
    const header = within(await screen.findByRole('banner'));
    await header.findByRole('heading', { level: 1, name: 'Участки с истёкшей арендой' });
    await header.findByLabelText(t.dialect);

    await user.click(header.getByRole('button', { name: /^Действия:/ }));
    await user.click(await screen.findByText(texts.chat.menu.rename));

    expect(await header.findByLabelText(texts.chat.titleLabel)).toHaveFocus();
    expect(header.getByLabelText(t.dialect)).toBeInTheDocument();
    expect(header.getByLabelText(t.schema)).toBeInTheDocument();
    expect(header.getByRole('switch', { name: t.testRun.label })).toBeInTheDocument();

    await user.keyboard('{Escape}');
    expect(await header.findByRole('heading', { level: 1, name: 'Участки с истёкшей арендой' })).toBeInTheDocument();
    expect(header.getByLabelText(t.dialect)).toBeInTheDocument();
  });
});

describe('SQL-помощник: проверка на тестовых данных', () => {
  const t = texts.sql;
  const r = t.run;
  const SCHEMAS = { 'GET /api/sql/schemas': () => ok({ items: [{ id: 's-1', name: 'Кадастр', updated_at: '2026-10-01T09:00:00Z' }] }) };
  const FIXTURE = [{ table: 'parcels', columns: ['id', 'area'], rows: [['1', '120.5'], ['2', null]], truncated: false }];
  const toggle = () => screen.getByRole('switch', { name: t.testRun.label });

  it('строка заголовка: диалект, схема, «Мои схемы», переключатель — в этом порядке; действие — в панели запроса после поля', async () => {
    const { user } = setup('sql', `/sql/${ID}`, [QUESTION, message({ content: 'Ответ' })], SCHEMAS);
    await screen.findByRole('switch', { name: t.testRun.label });
    const header = screen.getByRole('heading', { level: 1 }).closest('header') as HTMLElement;
    const order = [
      screen.getByLabelText(t.dialect),
      screen.getByLabelText(t.schema),
      screen.getByRole('button', { name: t.mySchemas }),
      toggle(),
      within(header).getByRole('button', { name: /^Действия/ }),
    ];
    order.forEach((element) => expect(header).toContainElement(element));
    order.slice(1).forEach((element, index) =>
      expect((order[index] as HTMLElement).compareDocumentPosition(element)).toBe(Node.DOCUMENT_POSITION_FOLLOWING),
    );
    // Название поля без подписи — первой строкой раскрытого списка.
    await user.click(screen.getByLabelText(t.schema));
    await waitFor(() => expect(document.querySelector('[data-tid="MenuHeader__root"]')).toHaveTextContent(t.schema));
    await user.keyboard('{Escape}');

    const input = field(t.placeholders.write);
    const action = screen.getByRole('switch', { name: t.actions.debug });
    expect(header).not.toContainElement(action);
    expect(input.compareDocumentPosition(action)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(action.compareDocumentPosition(screen.getByRole('button', { name: texts.chat.composer.send }))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    await user.click(action);
    expect(window.localStorage.getItem('portal.sql.action')).toBe('debug');
  });

  it('проверка выключена на портале: переключателя нет, строка под панелью — второй вариант, test_run: false', async () => {
    window.localStorage.setItem('portal.sql.test_run', 'on');
    window.localStorage.setItem('portal.sql.schema', 's-1');
    const { server, user } = setup('sql', `/sql/${ID}`, [], {
      ...SCHEMAS,
      'GET /api/config': () => ok({ ...CONFIG, sql: { ...CONFIG.sql, test_run: { enabled: false, dialect: 'postgres' } } }),
      [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['done', { status: 'complete' }]]),
    });

    expect(await screen.findByText(t.note.withoutRun)).toBeInTheDocument();
    expect(screen.queryByText(t.note.withRun)).not.toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: t.testRun.label })).not.toBeInTheDocument();
    await user.type(field(t.placeholders.write), 'Участки{Enter}');
    await waitFor(() => expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({ test_run: false }));
  });

  it('переключатель: без схемы и с непроверяемым диалектом недоступен, но в обходе и с причиной; выбор запоминается и не теряется', async () => {
    const { server, user } = setup('sql', `/sql/${ID}`, [], {
      ...SCHEMAS,
      [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')),
      [`POST /api/dialogs/${ID}/messages`]: () => sse([START, ['done', { status: 'complete' }]]),
    });
    const sent = () => server.callsTo(`POST /api/dialogs/${ID}/messages`).at(-1)?.body;

    // «Без схемы»: выключен, недоступен, фокус получает, причина связана с ним.
    await screen.findByText(t.note.withRun);
    expect(toggle()).toHaveAttribute('aria-disabled', 'true');
    expect(toggle()).toHaveAttribute('aria-checked', 'false');
    expect(toggle()).toHaveAttribute('tabindex', '0');
    expect(toggle()).toHaveAccessibleDescription(t.testRun.needSchema);

    // Схема выбрана: доступен, по умолчанию выключен; включение запоминается и уходит с вопросом.
    await user.click(screen.getByText(t.noSchema));
    await user.click(await screen.findByText('Кадастр'));
    expect(toggle()).not.toHaveAttribute('aria-disabled');
    expect(toggle()).not.toBeChecked();
    expect(toggle()).toHaveAccessibleDescription(t.testRun.hint);
    await user.click(toggle());
    expect(toggle()).toBeChecked();
    expect(window.localStorage.getItem('portal.sql.test_run')).toBe('on');
    await user.type(field(t.placeholders.write), 'Первый{Enter}');
    await waitFor(() => expect(sent()).toMatchObject({ content: 'Первый', schema_id: 's-1', test_run: true }));

    // Диалект, который не проверяется: недоступен, вопрос уходит без проверки.
    await user.click(screen.getByText('PostgreSQL + PostGIS'));
    await user.click(await screen.findByText('Microsoft SQL Server'));
    expect(toggle()).toHaveAttribute('aria-disabled', 'true');
    expect(toggle()).toHaveAccessibleDescription('Проверка работает только для PostgreSQL + PostGIS');
    await user.type(field(t.placeholders.write), 'Второй{Enter}');
    await waitFor(() => expect(sent()).toMatchObject({ content: 'Второй', dialect: 'mssql', test_run: false }));

    // Запомненное «включено» возвращается вместе с проверяемым диалектом.
    await user.click(screen.getByText('Microsoft SQL Server'));
    await user.click(await screen.findByText('PostgreSQL + PostGIS'));
    expect(toggle()).toBeChecked();
  });

  it('поток: ответ дописан по первому sql_run; на время проверки «Ответить заново» недоступно, «Остановить» есть; done возвращает панель', async () => {
    const { server, user } = setup('sql', `/sql/${ID}`, [], { [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')) });
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    await user.type(field(t.placeholders.write), 'Участки{Enter}');

    live.push(START, ['delta', { text: 'Первый:\n```sql\nSELECT 1\n```\nВторой:\n```sql\nSELECT 2\n```' }]);
    live.push(['sql_check', { blocks: [{ index: 0, line: 2, valid: true, error: null, dangers: [] }] }]);
    await screen.findAllByText(t.check.valid);
    // До первого sql_run блоков проверки нет, ответ ещё не считается дописанным.
    expect(screen.queryByText(r.running)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: texts.chat.exportAnswer })).not.toBeInTheDocument();

    live.push(['sql_run', { blocks: [runBlock({ index: 0, line: 2, status: 'running' }), runBlock({ index: 1, line: 6, status: 'pending' })] }]);
    expect(await screen.findByText(r.running)).toBeInTheDocument();
    expect(screen.getByText(r.pending)).toBeInTheDocument();
    // Итог синтаксиса стоит выше блока проверки.
    expect(screen.getByText(t.check.valid).compareDocumentPosition(screen.getByText(r.running))).toBe(
      Node.DOCUMENT_POSITION_FOLLOWING,
    );
    expect(screen.getByRole('button', { name: texts.chat.exportAnswer })).toBeInTheDocument();
    const regenerate = screen.getByRole('button', { name: texts.chat.regenerate });
    expect(regenerate).toHaveAttribute('aria-disabled', 'true');
    await user.click(regenerate);
    expect(server.callsTo(`POST /api/dialogs/${ID}/regenerate`)).toHaveLength(0);
    expect(screen.getByRole('button', { name: texts.chat.composer.stop })).toBeInTheDocument();
    // Оговорки у состояний без данных нет.
    expect(screen.queryByText(r.disclaimer)).not.toBeInTheDocument();

    live.push(
      [
        'sql_run',
        {
          blocks: [
            runBlock({ index: 0, line: 2, fixture: FIXTURE, result: { columns: ['n'], rows: [['1'], ['2'], ['3']], truncated: false } }),
            runBlock({ index: 1, line: 6, status: 'unavailable' }),
          ],
        },
      ],
      ['done', { status: 'complete' }],
    );
    live.close();
    expect(await screen.findByText(r.passed)).toBeInTheDocument();
    expect(screen.getByText(r.unavailable)).toBeInTheDocument();
    expect(screen.queryByText(r.running)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole('button', { name: texts.chat.composer.send })).toBeInTheDocument());
    expect(screen.getByRole('button', { name: texts.chat.regenerate })).not.toHaveAttribute('aria-disabled');
    // Одно объявление для экранного чтения — по завершении.
    expect(screen.getByText(`Проверка на тестовых данных: запрос выполнился, строк в результате: 3. ${r.unavailable}`)).toHaveAttribute(
      'role',
      'status',
    );
  });

  it('вопрос с проверкой: ответ дописан по sql_check — остановка до первого sql_run его не трогает; проверять нечего — «Ответ готов»', async () => {
    const { server, user } = setup('sql', `/sql/${ID}`, [], { ...SCHEMAS, [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')) });
    let live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByText(t.note.withRun);
    await user.click(screen.getByText(t.noSchema));
    await user.click(await screen.findByText('Кадастр'));
    await user.click(toggle());
    await user.type(field(t.placeholders.write), 'Участки{Enter}');
    await waitFor(() => expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toMatchObject({ test_run: true }));

    live.push(START, ['delta', { text: 'Запрос не нужен.' }], ['sql_check', { blocks: [] }]);
    // Ответ дописан: строка действий есть, хотя проверка ещё не началась и поток открыт.
    expect(await screen.findByRole('button', { name: texts.chat.exportAnswer })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: texts.chat.composer.stop }));
    await waitFor(() => expect(screen.getByRole('button', { name: texts.chat.composer.send })).toBeInTheDocument());
    expect(screen.queryByText(texts.chat.stopped)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.chat.exportAnswer })).toBeInTheDocument();

    // Закрытых блоков sql в ответе нет: сервер присылает пустую проверку, объявление — обычное.
    live = liveSse();
    await user.type(field(t.placeholders.write), 'Ещё{Enter}');
    live.push(['start', { user_message_id: 'u-2', assistant_message_id: 'a-2' }], ['delta', { text: 'Запрос не нужен.' }]);
    live.push(['sql_check', { blocks: [] }], ['sql_run', { blocks: [] }], ['done', { status: 'complete' }]);
    live.close();
    await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(texts.chat.phase.done));
  });

  it('«Остановить» во время проверки: ответ цел и не «остановлен», блок без итога — «Проверка не завершена»', async () => {
    const { server, user } = setup('sql', `/sql/${ID}`, [], { [`GET /api/dialogs/${ID}`]: () => ok(dialog('sql')) });
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    await user.type(field(t.placeholders.write), 'Участки{Enter}');
    live.push(START, ['delta', { text: '```sql\nSELECT 1\n```\n\n```sql\nSELECT 2\n```' }], ['sql_check', { blocks: [] }]);
    live.push(['sql_run', { blocks: [runBlock({ index: 0, line: 1, status: 'timeout' }), runBlock({ index: 1, line: 5, status: 'running' })] }]);
    await screen.findByText(r.running);

    // Esc в поле ввода — то же, что «Остановить».
    await user.click(field(t.placeholders.write));
    await user.keyboard('{Escape}');
    expect(await screen.findByText(r.stopped)).toBeInTheDocument();
    expect(screen.getByText(r.timeout)).toBeInTheDocument();
    expect(screen.queryByText(texts.chat.stopped)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.chat.composer.send })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.chat.regenerate })).not.toHaveAttribute('aria-disabled');
  });

  it('успех: оговорка, тестовые данные свёрнуты, результат развёрнут; значения из базы — только текст', async () => {
    const hostile = '<img src=x onerror=alert(1)>';
    const { user } = setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({
        content: '```sql\nSELECT id, area FROM parcels\n```',
        sql_run: {
          blocks: [
            runBlock({
              fixture: FIXTURE,
              result: { columns: ['id', hostile], rows: [['1', hostile], ['2', null]], truncated: true },
            }),
          ],
        },
      }),
    ]);

    expect(await screen.findByText(r.passed)).toBeInTheDocument();
    expect(screen.getByText(r.disclaimer)).toBeInTheDocument();
    const result = screen.getByRole('table', { name: r.resultCaption });
    expect(screen.getByText('Результат · 2 строки')).toBeInTheDocument();
    expect(within(result).getAllByText(hostile)).toHaveLength(2);
    expect(result.querySelector('img')).toBeNull();
    expect(within(result).getByText('NULL')).toBeInTheDocument();
    expect(screen.getByText('Показаны первые 2 строки')).toBeInTheDocument();
    // Область с прокруткой достижима с клавиатуры.
    expect(screen.getByRole('region', { name: r.resultCaption })).toHaveAttribute('tabindex', '0');

    const fixture = screen.getByRole('button', { name: 'Тестовые данные · 1 таблица' });
    expect(fixture).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('table', { name: 'Тестовая таблица parcels' })).not.toBeInTheDocument();
    await user.click(fixture);
    expect(fixture).toHaveAttribute('aria-expanded', 'true');
    expect(within(screen.getByRole('table', { name: 'Тестовая таблица parcels' })).getByText('120.5')).toBeInTheDocument();
  });

  it('исправлено и ошибка: сообщение базы как есть, исправленный запрос со своим «Скопировать», исходный блок отмечен', async () => {
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({
        content: '```sql\nSELECT aera FROM parcels\n```\n\n```sql\nSELECT * FROM nowhere\n```',
        sql_run: {
          blocks: [
            runBlock({
              status: 'fixed',
              fixture: FIXTURE,
              error: 'column "aera" does not exist',
              fixed_sql: 'SELECT area FROM parcels',
              result: { columns: ['area'], rows: [], truncated: false },
            }),
            runBlock({ index: 1, line: 5, status: 'failed', error: 'relation "nowhere" does not exist' }),
          ],
        },
      }),
    ]);

    expect(await screen.findByText(r.fixed)).toBeInTheDocument();
    expect(screen.getByText(r.superseded)).toBeInTheDocument();
    expect(screen.getByText(r.originalError)).toBeInTheDocument();
    expect(screen.getByText('column "aera" does not exist')).toHaveAttribute('lang', 'en');
    const blocks = [...document.querySelectorAll('pre:not([lang])')];
    expect(blocks.map((block) => block.textContent)).toEqual([
      'SELECT aera FROM parcels',
      'SELECT area FROM parcels',
      'SELECT * FROM nowhere',
    ]);
    // Исправленный запрос выполнился, но данных не вернул: пояснение вместо таблицы результата.
    expect(screen.getByText(r.emptyReadText)).toBeInTheDocument();
    expect(screen.queryByRole('table')).not.toBeInTheDocument();

    expect(screen.getByText(r.failed)).toBeInTheDocument();
    expect(screen.getByText(r.failedText)).toBeInTheDocument();
    expect(screen.getByText(r.dbMessage)).toBeInTheDocument();
    expect(screen.getByText('relation "nowhere" does not exist')).toBeInTheDocument();
    // Оговорка — в обоих блоках с рамкой; у ошибки без тестовых данных их кнопки нет.
    expect(screen.getAllByText(r.disclaimer)).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: /^Тестовые данные/ })).toHaveLength(1);
  });

  it.each<[string, Partial<SqlRunBlock>, string[], string[]]>([
    ['изменяющий запрос', { affected_rows: 3 }, [r.passed, 'Затронуто строк: 3. Изменения отменены — проверка ничего не сохраняет'], [r.emptyWrite]],
    ['пусто, чтение', { result: { columns: ['id'], rows: [], truncated: false } }, [r.emptyRead, r.emptyReadText, r.disclaimer], [r.passed]],
    ['пусто, изменение', { affected_rows: 0 }, [r.emptyWrite, r.emptyWriteText, r.disclaimer], [r.passed]],
    ['не проверялось: причина', { status: 'not_checked', reason: 'multiple_statements' }, [r.reasons.multiple_statements as string], [r.disclaimer]],
    ['не проверялось: одно имя таблицы с разными схемами', { status: 'not_checked', reason: 'table_name_conflict' }, [r.reasons.table_name_conflict as string], [r.disclaimer]],
    ['не проверялось: неизвестная причина', { status: 'not_checked', reason: 'new_reason' }, [r.notChecked], [r.disclaimer]],
    ['неизвестное состояние', { status: 'new_status' as SqlRunBlock['status'] }, [r.notChecked], [r.disclaimer]],
    ['после перезагрузки — не завершена', { status: 'stopped' }, [r.stopped], [r.disclaimer]],
  ])('состояние блока: %s', async (_name, patch, present, absent) => {
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({ content: '```sql\nUPDATE parcels SET area = 1\n```', sql_run: { blocks: [runBlock({ fixture: FIXTURE, ...patch })] } }),
    ]);

    // Однострочный итог повторяется в объявлении для экранного чтения — оно здесь не в счёт.
    for (const text of present) {
      expect(await screen.findByText(text, { ignore: '[role="status"]' })).toBeInTheDocument();
    }
    absent.forEach((text) => expect(screen.queryByText(text)).not.toBeInTheDocument());
  });

  it('итог без блока на его строке не теряется: под текстом ответа, с пояснением', async () => {
    setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({ content: '```sql\nSELECT 1\n```\n\nТекст', sql_run: { blocks: [runBlock({ line: 40, status: 'failed', error: 'boom' })] } }),
    ]);

    const note = await screen.findByText(t.check.orphan);
    const failed = screen.getByText(r.failed);
    expect(screen.getByText('Текст').compareDocumentPosition(note)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(note.compareDocumentPosition(failed)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
    expect(screen.queryByText(r.superseded)).not.toBeInTheDocument();
  });
});

describe('помощник CoGIS', () => {
  const t = texts.cogis;

  it('действие уходит с вопросом; фазы поиска; источники как в чате; своя строка для пустого поиска', async () => {
    const { server, user } = setup('cogis', '/cogis', [], {
      'POST /api/dialogs': () => ok(dialog('cogis', { title: null }), 201),
    });
    const live = liveSse();
    server.on(`POST /api/dialogs/${ID}/messages`, () => live.response);

    expect(await screen.findByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: t.empty.title })).toBeInTheDocument();
    expect(screen.getByText(t.note)).toBeInTheDocument();
    expect(screen.queryByText(t.noDocumentation)).not.toBeInTheDocument();

    await user.click(screen.getByRole('switch', { name: t.actions.debug }));
    await user.type(field(t.placeholders.debug), 'CS0103: имя не существует{Enter}');
    await waitFor(() =>
      expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toEqual({ content: 'CS0103: имя не существует', action: 'debug' }),
    );
    expect(server.callsTo('POST /api/dialogs')[0]?.body).toEqual({ kind: 'cogis' });

    live.push(START, ['search_started', {}]);
    expect((await screen.findAllByText(/Ищу в базе знаний/)).length).toBeGreaterThan(0);
    live.push(['sources', { sources: [] }]);
    expect(await screen.findByText(t.nothingFound)).toBeInTheDocument();
    expect(screen.queryByText(texts.chat.sources.nothingFound.withoutTags)).not.toBeInTheDocument();
    live.push(['delta', { text: 'Ответ по общим знаниям.' }], ['done', { status: 'complete' }]);
    live.close();
    expect(await screen.findByText('Ответ по общим знаниям.')).toBeInTheDocument();
  });

  it('раздел «Код» включили, пока экран открыт: 409 dialog_read_only — уведомление и переход в раздел (§5.13.15)', async () => {
    const { server, user } = setup('cogis', `/cogis/${ID}`, [QUESTION, message({ content: 'Ответ' })], {
      [`POST /api/dialogs/${ID}/messages`]: () => fail(409, 'dialog_read_only'),
    });

    const input = await screen.findByPlaceholderText(t.placeholders.write);
    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(1));
    server.on('GET /api/config', () => ok({ ...CONFIG, code: { ...CONFIG.code, enabled: true } }));
    await user.type(input, 'Ещё вопрос{Enter}');
    expect(await screen.findByText(texts.workshop.old.readOnly)).toBeInTheDocument();
    await waitFor(() => expect(path()).toBe(`/code/dialogs/${ID}`));
  });

  it('пока настройки не пришли, прежний экран не открывается и ничего не запрашивает; раздел «Код» включён — сразу туда', async () => {
    let release: (response: ReturnType<typeof ok>) => void = () => undefined;
    const kinds: (string | null)[] = [];
    const { server } = setup('cogis', '/cogis', [], {
      'GET /api/config': () => new Promise((resolve) => (release = resolve)),
      'GET /api/dialogs': (_body, url) => {
        kinds.push(url.searchParams.get('kind'));
        return ok({ items: [], next_cursor: null });
      },
      'GET /api/code/projects': () => ok({ items: [], next_cursor: null }),
      'GET /api/code/sdk': () => fail(503, 'sdk_unavailable'),
    });

    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(1));
    expect(screen.queryByRole('heading', { level: 1, name: t.title })).not.toBeInTheDocument();
    // Only requests made before the settings arrive count: in «Код» the old dialogs group lists them by right.
    expect(kinds).not.toContain('cogis');
    release(ok({ ...CONFIG, code: { ...CONFIG.code, enabled: true } }));
    await waitFor(() => expect(path()).toBe('/code'));
    // Only the old screen asks about CoGIS documentation.
    expect(server.callsTo('GET /api/kb/cogis-documentation')).toHaveLength(0);
  });

  it('ответ с источниками: кнопки-сноски и карточки', async () => {
    const source = { n: 1, document_id: 'k-1', document_title: 'Руководство CoGIS SDK.pdf', scope: 'shared' as const, page: 12, timecode_seconds: null, image_id: null, fragment_id: 'f-1', quote: 'Плагин реализует интерфейс IPlugin…' };
    setup('cogis', `/cogis/${ID}`, [QUESTION, message({ content: 'Реализуйте IPlugin [1].', sources_found: 3, sources: [source] })]);

    expect(await screen.findByRole('button', { name: 'Источник 1: Руководство CoGIS SDK.pdf, страница 12' })).toBeInTheDocument();
    expect(screen.getByText('Плагин реализует интерфейс IPlugin…')).toBeInTheDocument();
  });

  it('документации CoGIS нет — предупреждение с переходом к добавлению: общая база и отметка CoGIS', async () => {
    const { user } = setup('cogis', '/cogis', [], { 'GET /api/kb/cogis-documentation': () => ok({ available: false }) });

    expect(await screen.findByText(t.noDocumentation)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: t.addDocumentation }));
    await waitFor(() => expect(path()).toBe('/knowledge'));
    const modal = within(await screen.findByRole('dialog'));
    expect(modal.getByLabelText(texts.kb.upload.toShared)).toBeChecked();
    expect(modal.getByLabelText(texts.kb.upload.cogis)).toBeChecked();
  });

  it('сбой запроса признака — предупреждения нет', async () => {
    setup('cogis', '/cogis', [], { 'GET /api/kb/cogis-documentation': () => fail(500, 'internal_error') });
    await screen.findByRole('heading', { level: 2, name: t.empty.title });
    expect(screen.queryByText(t.noDocumentation)).not.toBeInTheDocument();
  });
});

describe('скачать в DOCX', () => {
  const c = texts.chat;
  let downloads: { name: string; href: string }[];
  let attachedOnClick: boolean[];

  beforeEach(() => {
    downloads = [];
    attachedOnClick = [];
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:docx');
    vi.spyOn(URL, 'revokeObjectURL').mockReturnValue(undefined);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push({ name: this.download, href: this.href });
      attachedOnClick.push(this.isConnected);
    });
  });

  function docx(name?: string) {
    return {
      status: 200,
      file: 'PK',
      headers: name
        ? { 'Content-Disposition': `attachment; filename="dialog.docx"; filename*=UTF-8''${encodeURIComponent(name)}` }
        : undefined,
    };
  }

  it('под ответом — один ответ; в меню — диалог целиком; имя файла — из заголовка сервера', async () => {
    const { server, user } = setup('sql', `/sql/${ID}`, [
      QUESTION,
      message({ id: 'a-0', content: 'Ответ с текстом' }),
      message({ id: 'a-1', content: '', status: 'stopped' }),
    ]);
    server.on(`GET /api/dialogs/${ID}/export`, (_body, url) => docx(url.searchParams.has('message_id') ? 'Ответ.docx' : 'Участки с истёкшей арендой.docx'));

    // Ссылка есть только у ответа с текстом.
    const links = await screen.findAllByRole('button', { name: c.exportAnswer });
    expect(links).toHaveLength(1);
    await user.click(links[0] as HTMLElement);
    expect(await screen.findByText(c.exporting)).toBeInTheDocument();
    await waitFor(() => expect(downloads).toEqual([{ name: 'Ответ.docx', href: 'blob:docx' }]));
    // Ссылка на время нажатия стоит в документе, адрес файла сразу не отзывается (Firefox, Safari).
    expect(attachedOnClick).toEqual([true]);
    expect(document.querySelector('a[download]')).toBeNull();
    expect(URL.revokeObjectURL).not.toHaveBeenCalled();
    expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input)).filter((url) => url.includes('/export'))).toEqual([
      `/api/dialogs/${ID}/export?message_id=a-0`,
    ]);

    await user.click(within(screen.getByRole('main')).getAllByRole('button', { name: /^Действия:/ }).at(-1) as HTMLElement);
    await user.click(menuItems().find((item) => item.textContent === c.menu.exportDocx) as HTMLElement);
    await waitFor(() => expect(downloads.at(-1)?.name).toBe('Участки с истёкшей арендой.docx'));
    expect(vi.mocked(fetch).mock.calls.map(([input]) => String(input)).at(-1)).toBe(`/api/dialogs/${ID}/export`);
  });

  it('заголовка нет — «диалог.docx»; сбой, удалённый диалог и пустой диалог — свои уведомления', async () => {
    const { server, user } = setup('cogis', `/cogis/${ID}`, [QUESTION, message({ content: 'Ответ' })]);
    const click = async () => user.click((await screen.findAllByRole('button', { name: c.exportAnswer }))[0] as HTMLElement);

    server.on(`GET /api/dialogs/${ID}/export`, () => docx());
    await click();
    await waitFor(() => expect(downloads.at(-1)?.name).toBe('диалог.docx'));

    for (const [status, code, text] of [
      [500, 'internal_error', c.exportFailed],
      [404, 'not_found', c.exportDeleted],
      [409, 'nothing_to_export', c.exportNothing],
    ] as const) {
      server.on(`GET /api/dialogs/${ID}/export`, () => fail(status, code));
      await click();
      expect(await screen.findByText(text)).toBeInTheDocument();
    }
    expect(downloads).toHaveLength(1);
  });

  it('в чате ссылка под ответом и пункт в меню истории тоже есть', async () => {
    mockApi({
      'GET /api/auth/session': () => ok(session('ready')),
      'GET /api/dialogs': (_body, url) => ok({ items: url.searchParams.get('kind') === 'chat' ? [dialog('chat')] : [], next_cursor: null }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [message({ content: 'Ответ' }), QUESTION], next_cursor: null }),
    });
    renderApp(`/chat/${ID}`);
    const user = userEvent.setup();

    expect(await screen.findByRole('button', { name: c.exportAnswer })).toBeInTheDocument();
    const history = within(screen.getByRole('complementary', { name: c.history }));
    await user.click(await history.findByRole('button', { name: /^Действия:/ }));
    await waitFor(() => expect(menuItems().map((item) => item.textContent)).toEqual(['Переименовать', 'Скачать в DOCX', 'Удалить']));
  });
});

describe('разбор документов', () => {
  const t = texts.docparse;
  const FIELDS = [
    { title: 'Кадастровый номер', value: '77:01:0004012:345' },
    { title: 'Площадь', value: '1 250 кв. м' },
    { title: 'Правообладатель', value: null },
  ];

  beforeEach(() => {
    vi.stubGlobal(
      'DataTransfer',
      class {
        items = { add: () => undefined };
        files = null;
      },
    );
  });

  function result(patch: Partial<DocparseResult> = {}): DocparseResult {
    return {
      file_name: 'выписка_77-01.pdf',
      page_count: 3,
      template_id: 'egrn',
      template_title: 'Выписка ЕГРН',
      free_form: false,
      fields: FIELDS,
      summary: 'Выписка подтверждает право собственности.',
      summary_status: 'complete',
      ...patch,
    };
  }

  function setupDocs(pathname: string, extra: Parameters<typeof mockApi>[0] = {}) {
    const server = mockApi({ 'GET /api/auth/session': () => ok(session('ready')), ...extra });
    renderApp(pathname);
    return { server, user: userEvent.setup() };
  }

  async function pickFile(name = 'выписка_77-01.pdf', size?: number) {
    await screen.findByText(t.pick);
    const file = new File(['%PDF-'], name);
    if (size) {
      Object.defineProperty(file, 'size', { value: size });
    }
    fireEvent.change(document.querySelector('input[type="file"]') as HTMLInputElement, { target: { files: [file] } });
  }

  it('форма: ничего не предвыбрано, ошибки пустой формы и файла, файл уходит по кнопке «Разобрать»', async () => {
    const { server, user } = setupDocs('/documents');
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);

    expect(await screen.findByRole('heading', { level: 1, name: t.title })).toBeInTheDocument();
    expect(await screen.findByText('Скан, фото, PDF или DOCX — до 50 МБ, PDF до 40 страниц.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.dialogs.docparse.newLabel })).toBeInTheDocument();
    for (const radio of screen.getAllByRole('radio')) {
      expect(radio).not.toBeChecked();
    }
    expect(screen.getByText(/Без таблицы реквизитов заранее/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: t.submit }));
    expect(screen.getByText(t.chooseFile)).toBeInTheDocument();
    expect(screen.getByText(t.chooseTemplate)).toBeInTheDocument();

    await pickFile('заметки.txt');
    expect(await screen.findByText(texts.kb.upload.unsupported)).toBeInTheDocument();
    await pickFile('огромный.pdf', 52428801);
    expect(await screen.findByText('Файл больше 50 МБ')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Убрать файл огромный.pdf' }));
    expect(screen.queryByText('огромный.pdf')).not.toBeInTheDocument();

    await pickFile();
    expect(await screen.findByText('выписка_77-01.pdf')).toBeInTheDocument();
    expect(server.callsTo('POST /api/docparse')).toHaveLength(0);
    await user.click(screen.getByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));

    expect(await screen.findByText(t.uploading)).toBeInTheDocument();
    const form = server.callsTo('POST /api/docparse')[0]?.body as FormData;
    expect([(form.get('file') as File).name, form.get('template_id')]).toEqual(['выписка_77-01.pdf', 'egrn']);
    expect(screen.getByText(t.keepTab)).toBeInTheDocument();
  });

  it('ход по событиям: страницы → реквизиты → таблица (адрес и история) → содержание → вопросы', async () => {
    const { server, user } = setupDocs('/documents');
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);
    server.on(`GET /api/dialogs/${ID}/messages`, () => ok({ items: [], next_cursor: null }));
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse', { title: 'выписка_77-01.pdf' }), docparse: result({ summary: '', summary_status: 'streaming' }) }));

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));

    live.push(['progress', { page_from: 9, page_to: 16, pages_total: 40 }]);
    expect(await screen.findByText('Читаю документ… страницы 9–16 из 40')).toBeInTheDocument();
    // В рейке у «Документы» — индикатор вместо иконки.
    expect(screen.getByRole('link', { name: texts.nav.documents }).querySelector('[data-tid="Spinner__root"]')).not.toBeNull();
    live.push(['extraction_started', {}]);
    expect(await screen.findByText(t.extracting)).toBeInTheDocument();

    live.push(['extraction', { dialog_id: ID, fields: FIELDS }]);
    await waitFor(() => expect(path()).toBe(`/documents/${ID}`));
    expect(await screen.findByRole('heading', { level: 1, name: 'выписка_77-01.pdf · Выписка ЕГРН' })).toBeInTheDocument();
    expect(screen.getByText(t.verify)).toBeInTheDocument();
    expect(screen.queryByText(t.keepTab)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'выписка_77-01.pdf' })).toHaveAttribute('aria-current', 'page');

    const table = within(screen.getByRole('table'));
    expect(table.getByText('77:01:0004012:345')).toHaveClass('p-mono');
    expect(table.getByText('1 250 кв. м')).not.toHaveClass('p-mono');
    const empty = table.getByRole('row', { name: /Правообладатель/ });
    expect(within(empty).getByText(t.absent)).toBeInTheDocument();
    expect(within(empty).queryByRole('button')).not.toBeInTheDocument();
    expect(table.getByRole('button', { name: 'Скопировать: Кадастровый номер' })).toBeInTheDocument();
    expect(screen.getByText(t.writingSummary)).toBeInTheDocument();

    // Пока содержание пишется, вопрос не отправляется; «Остановить» относится к содержанию.
    live.push(['delta', { text: 'Выписка подтверждает ' }]);
    expect(await screen.findByText('Выписка подтверждает')).toBeInTheDocument();
    await user.type(field(t.placeholder), 'Кто правообладатель?{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.waitSummary);
    expect(screen.getByRole('button', { name: texts.chat.composer.stop })).toBeInTheDocument();

    // После `done` разбор перечитывается: число страниц и очищенное имя файла знает только сервер.
    server.on(`GET /api/dialogs/${ID}`, () =>
      ok({ ...dialog('docparse'), docparse: result({ file_name: 'выписка 77-01.pdf', summary: 'Выписка подтверждает право собственности.' }) }),
    );
    live.push(['delta', { text: 'право собственности.' }], ['done', { status: 'complete' }]);
    live.close();
    expect(await screen.findByText('Выписка подтверждает право собственности.')).toBeInTheDocument();
    expect(await screen.findByRole('heading', { level: 1, name: 'выписка 77-01.pdf · Выписка ЕГРН' })).toBeInTheDocument();
    await screen.findByRole('button', { name: texts.chat.composer.send });
    expect(screen.getByRole('link', { name: texts.nav.documents }).querySelector('[data-tid="Spinner__root"]')).toBeNull();

    // Вопрос по документу — обычное сообщение диалога: только `content`.
    server.on(`POST /api/dialogs/${ID}/messages`, () => sse([START, ['delta', { text: 'В документе не указан.' }], ['done', { status: 'complete' }]]));
    await user.click(screen.getByRole('button', { name: texts.chat.composer.send }));
    expect(await screen.findByText('В документе не указан.')).toBeInTheDocument();
    expect(server.callsTo(`POST /api/dialogs/${ID}/messages`)[0]?.body).toEqual({ content: 'Кто правообладатель?' });
  });

  it('остановка до таблицы возвращает форму с тем же файлом и шаблоном и строкой «Разбор остановлен»', async () => {
    const { server, user } = setupDocs('/documents');
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    live.push(['progress', { page_from: 1, page_to: 3, pages_total: 3 }]);
    await screen.findByText('Читаю документ… страницы 1–3 из 3');

    await user.click(screen.getByRole('button', { name: t.stop }));
    expect(await screen.findByText(t.stopped)).toBeInTheDocument();
    expect(screen.getByText('выписка_77-01.pdf')).toBeInTheDocument();
    expect(screen.getByLabelText(/Выписка ЕГРН/)).toBeChecked();
    expect(path()).toBe('/documents');
  });

  it.each([
    ['recognition_failed', undefined, t.errors.recognition, false],
    ['document_too_long', undefined, t.tooLong, false],
    ['generation_timeout', undefined, t.errors.timeout, true],
    ['model_unavailable', { page: 17, pages_total: 40 }, 'Разбор оборвался на странице 17 из 40. Запустите его заново', true],
    ['internal_error', undefined, t.errors.broken, true],
    ['model_overloaded', undefined, t.errors.overloaded, true],
  ] as const)('сбой до таблицы %s: форма с заметкой', async (code, details, text, retry) => {
    const { server, user } = setupDocs('/documents');
    server.on('POST /api/docparse', () => sse([['error', { code, message: 'x', ...(details ? { details } : {}) }]]));

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));

    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByText('выписка_77-01.pdf')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: t.retry }) !== null).toBe(retry);
    if (retry) {
      await user.click(screen.getByRole('button', { name: t.retry }));
      await waitFor(() => expect(server.callsTo('POST /api/docparse')).toHaveLength(2));
    }
  });

  it.each([
    [422, 'too_many_pages', { max_pages: 40 }, 'В документе больше 40 страниц. Разделите его на части и разберите их по отдельности'],
    [422, 'document_too_long', undefined, t.tooLong],
    [422, 'file_unreadable', undefined, texts.kb.upload.unreadable],
    [422, 'docx_too_large', undefined, 'В документе слишком много содержимого помимо картинок. Разделите его на части'],
    [422, 'docx_images_too_large', undefined, 'Картинки в документе занимают слишком много места. Уменьшите их или разделите документ на части'],
    [415, 'unsupported_file_type', undefined, texts.kb.upload.unsupported],
  ])('отказ до начала разбора %s %s — под областью выбора файла', async (status, code, details, text) => {
    const { server, user } = setupDocs('/documents');
    server.on('POST /api/docparse', () => fail(status, code, { details }));

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    expect(await screen.findByRole('alert')).toHaveTextContent(text);
    expect(screen.getByRole('button', { name: t.submit })).toBeInTheDocument();
  });

  it('событие error с кодом вне словаря — текст сервера и «Разобрать заново»; недоступная модель — «Разбор оборвался»', async () => {
    const { server, user } = setupDocs('/documents');
    server.on('POST /api/docparse', () => sse([['error', { code: 'new_code', message: 'Текст сервера о новой причине.' }]]));
    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    expect(await screen.findByText('Текст сервера о новой причине.')).toBeInTheDocument();

    server.on('POST /api/docparse', () => sse([['error', { code: 'model_unavailable', message: 'Модель недоступна.' }]]));
    await user.click(screen.getByRole('button', { name: t.retry }));
    expect(await screen.findByText(t.errors.broken)).toBeInTheDocument();
  });

  it('настройки не загрузились: вместо шаблонов — заметка с «Повторить»; после загрузки выбранный файл на месте', async () => {
    const { server, user } = setupDocs('/documents', { 'GET /api/config': () => fail(503, 'service_unavailable') });

    expect(await screen.findByText(texts.common.configFailed)).toBeInTheDocument();
    expect(screen.queryAllByRole('radio')).toHaveLength(0);
    await pickFile('любой.xyz');
    expect(await screen.findByText('любой.xyz')).toBeInTheDocument();

    // «Разобрать» без шаблонов не молчит: заметка на месте, загрузка настроек повторяется сразу.
    await user.click(screen.getByRole('button', { name: t.submit }));
    await waitFor(() => expect(server.callsTo('GET /api/config')).toHaveLength(2));
    expect(screen.queryByText(t.chooseTemplate)).not.toBeInTheDocument();

    server.on('GET /api/config', () => ok(CONFIG));
    await user.click(screen.getByRole('button', { name: texts.common.retry }));
    expect(await screen.findByRole('radiogroup', { name: t.stepTemplate })).toBeInTheDocument();
    expect(screen.queryByText(texts.common.configFailed)).not.toBeInTheDocument();
    expect(screen.getByText('любой.xyz')).toBeInTheDocument();
  });

  it('обрыв сети до таблицы — заметка; после таблицы — «Краткое содержание остановлено» и уведомление', async () => {
    const { server, user } = setupDocs('/documents');
    server.on('POST /api/docparse', () => {
      throw new TypeError('Failed to fetch');
    });
    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    expect(await screen.findByText(t.errors.network)).toBeInTheDocument();

    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);
    server.on(`GET /api/dialogs/${ID}/messages`, () => ok({ items: [], next_cursor: null }));
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: '', summary_status: 'streaming' }) }));
    await user.click(screen.getByRole('button', { name: t.retry }));
    live.push(['extraction', { dialog_id: ID, fields: FIELDS }], ['delta', { text: 'Начало' }]);
    await screen.findByText('Начало');
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: 'Начало', summary_status: 'stopped' }) }));
    live.fail();
    expect(await screen.findByText(t.summaryStopped)).toBeInTheDocument();
    expect(await screen.findByText(t.connectionLost)).toBeInTheDocument();
    expect(screen.getByText('Начало')).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
  });

  it('обрыв сети после таблицы: разбор перечитывается — сервер мог дописать содержание', async () => {
    const { server, user } = setupDocs('/documents');
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);
    server.on(`GET /api/dialogs/${ID}/messages`, () => ok({ items: [], next_cursor: null }));
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: 'Начало и конец.' }) }));
    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    live.push(['extraction', { dialog_id: ID, fields: FIELDS }], ['delta', { text: 'Начало' }]);
    await screen.findByText('Начало');
    live.fail();

    expect(await screen.findByText('Начало и конец.')).toBeInTheDocument();
    expect(screen.getByText(t.connectionLost)).toBeInTheDocument();
    expect(screen.queryByText(t.summaryStopped)).not.toBeInTheDocument();
  });

  it('содержание другого разбора пишется в другой вкладке, пока здесь идёт свой: чужой перезапрашивается', async () => {
    const other = dialog('docparse', { id: 'd-2', title: 'договор.pdf' });
    let summary = result({ file_name: 'договор.pdf', summary: '', summary_status: 'streaming' });
    const { server, user } = setupDocs('/documents', {
      'GET /api/dialogs': (_body, url) => ok({ items: url.searchParams.get('kind') === 'docparse' ? [other] : [], next_cursor: null }),
      'GET /api/dialogs/d-2': () => ok({ ...other, docparse: summary }),
      'GET /api/dialogs/d-2/messages': () => ok({ items: [], next_cursor: null }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
    });
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);
    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    live.push(['extraction', { dialog_id: ID, fields: FIELDS }]);
    await waitFor(() => expect(path()).toBe(`/documents/${ID}`));

    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      await user.click(screen.getByRole('link', { name: 'договор.pdf' }));
      expect(await screen.findByText(t.summaryElsewhere)).toBeInTheDocument();
      summary = result({ file_name: 'договор.pdf', summary: 'Договор дописан.' });
      vi.advanceTimersByTime(5000);
      expect(await screen.findByText('Договор дописан.')).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
      live.close();
    }
  });

  it.each([
    ['model_overloaded', t.summaryOverloaded],
    ['generation_timeout', t.summaryError],
  ])('сбой после таблицы (%s): таблица остаётся, заметка у краткого содержания', async (code, text) => {
    const { server, user } = setupDocs('/documents');
    server.on(`GET /api/dialogs/${ID}/messages`, () => ok({ items: [], next_cursor: null }));
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: '', summary_status: 'streaming' }) }));
    // Сервер к этому моменту уже сохранил состояние `error`.
    server.on('POST /api/docparse', () => {
      server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: '', summary_status: 'error' }) }));
      return sse([['extraction', { dialog_id: ID, fields: FIELDS }], ['error', { code, message: 'x' }]]);
    });

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    expect(await screen.findByText(text)).toBeInTheDocument();
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: texts.chat.composer.send })).toBeInTheDocument();
  });

  it('разбор удалён в другой вкладке во время содержания: форма нового разбора, строка убрана, уведомление', async () => {
    const { server, user } = setupDocs('/documents');
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);
    server.on(`GET /api/dialogs/${ID}/messages`, () => ok({ items: [], next_cursor: null }));
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: '', summary_status: 'streaming' }) }));

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    live.push(['extraction', { dialog_id: ID, fields: FIELDS }]);
    await screen.findByRole('table');

    live.push(['error', { code: 'dialog_deleted', message: 'Удалён.' }]);
    live.close();
    expect(await screen.findByText(texts.dialogs.docparse.removedElsewhere)).toBeInTheDocument();
    await waitFor(() => expect(path()).toBe('/documents'));
    expect(await screen.findByText(t.pick)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'выписка_77-01.pdf' })).not.toBeInTheDocument();
  });

  it('таблица готова, пока сотрудник в другом разделе: уведомление с «Открыть»; поток не прерван', async () => {
    const { server, user } = setupDocs('/documents');
    const live = liveSse();
    server.on('POST /api/docparse', () => live.response);
    server.on(`GET /api/dialogs/${ID}/messages`, () => ok({ items: [], next_cursor: null }));
    server.on(`GET /api/dialogs/${ID}`, () => ok({ ...dialog('docparse'), docparse: result({ summary: '', summary_status: 'streaming' }) }));

    await pickFile();
    await user.click(await screen.findByLabelText(/Выписка ЕГРН/));
    await user.click(screen.getByRole('button', { name: t.submit }));
    await screen.findByText(t.uploading);

    await user.click(screen.getByRole('link', { name: texts.nav.knowledge }));
    await screen.findByRole('heading', { level: 1, name: texts.kb.title });
    live.push(['extraction', { dialog_id: ID, fields: FIELDS }], ['delta', { text: 'Содержание из потока' }]);
    expect(await screen.findByText(t.tableReady)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: t.open }));
    await waitFor(() => expect(path()).toBe(`/documents/${ID}`));
    expect(await screen.findByText('Содержание из потока')).toBeInTheDocument();
  });

  it.each<[Partial<DocparseResult>, string | null]>([
    [{ summary_status: 'complete' }, null],
    [{ summary_status: 'length_limit' }, t.summaryLengthLimit],
    [{ summary_status: 'stopped', summary: '' }, t.summaryStopped],
    [{ summary_status: 'error' }, t.summaryError],
  ])('сохранённый разбор %o', async (patch, text) => {
    setupDocs(`/documents/${ID}`, {
      'GET /api/dialogs': (_body, url) => ok({ items: url.searchParams.get('kind') === 'docparse' ? [dialog('docparse', { title: 'выписка_77-01.pdf' })] : [], next_cursor: null }),
      [`GET /api/dialogs/${ID}`]: () => ok({ ...dialog('docparse'), docparse: result(patch) }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
    });

    expect(await screen.findByRole('heading', { level: 2, name: t.fields })).toBeInTheDocument();
    if (text) {
      expect(screen.getByText(text)).toBeInTheDocument();
    }
    if (patch.summary !== '') {
      expect(screen.getByText('Выписка подтверждает право собственности.')).toBeInTheDocument();
    }
    expect(screen.getByRole('button', { name: texts.chat.composer.send })).toBeInTheDocument();
  });

  it('содержание пишется в другой вкладке — только индикатор; произвольный документ — «Главное из документа»', async () => {
    const { user } = setupDocs(`/documents/${ID}`, {
      [`GET /api/dialogs/${ID}`]: () => ok({ ...dialog('docparse'), docparse: result({ free_form: true, summary: 'Часть', summary_status: 'streaming' }) }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
    });

    expect(await screen.findByRole('heading', { level: 2, name: t.fieldsFree })).toBeInTheDocument();
    expect(screen.getByText(t.summaryElsewhere)).toBeInTheDocument();
    expect(screen.queryByText('Часть')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: texts.chat.composer.stop })).not.toBeInTheDocument();
    await user.type(field(t.placeholder), 'Вопрос{Enter}');
    expect(screen.getByRole('alert')).toHaveTextContent(t.waitSummary);
  });

  it('слова раздела: «по этому документу уже формируется ответ»; заметка о длинном диалоге без действия', async () => {
    const words = texts.dialogs.docparse;
    const { user } = setupDocs(`/documents/${ID}`, {
      [`GET /api/dialogs/${ID}`]: () => ok({ ...dialog('docparse'), docparse: result() }),
      [`GET /api/dialogs/${ID}/messages`]: () =>
        ok({ items: [message({ content: 'Ответ', dropped_messages: 3 }), QUESTION], next_cursor: null }),
      [`POST /api/dialogs/${ID}/messages`]: () => fail(409, 'generation_in_progress'),
    });

    expect(
      await screen.findByText(
        'Вопросов по документу стало много: самые ранние вопросы и ответы модель больше не учитывает. Сам документ и таблицу реквизитов она учитывает по-прежнему',
      ),
    ).toBeInTheDocument();
    // Кнопка «Новый разбор» — только в панели истории: у заметки действия нет.
    expect(screen.getAllByRole('button', { name: words.newLabel })).toHaveLength(1);
    await user.type(field(t.placeholder), 'Вопрос{Enter}');
    expect(await screen.findByText(words.inProgress)).toBeInTheDocument();
  });

  it('меню разбора: «Скачать в DOCX» и «Удалить», переименования нет; чужой разбор — «Разбор не найден»', async () => {
    const { server, user } = setupDocs(`/documents/${ID}`, {
      [`GET /api/dialogs/${ID}`]: () => ok({ ...dialog('docparse'), docparse: result() }),
      [`GET /api/dialogs/${ID}/messages`]: () => ok({ items: [], next_cursor: null }),
      [`DELETE /api/dialogs/${ID}`]: () => ok(),
    });

    await user.click(await screen.findByRole('button', { name: 'Действия: выписка_77-01.pdf' }));
    expect(menuItems().map((item) => item.textContent)).toEqual(['Скачать в DOCX', 'Удалить']);
    await user.click(menuItems()[1] as HTMLElement);
    expect(await screen.findByText(/Удалить разбор/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Удалить' }));
    expect(await screen.findByText(texts.dialogs.docparse.removed)).toBeInTheDocument();
    await waitFor(() => expect(path()).toBe('/documents'));

    server.on('GET /api/dialogs/d-x', () => fail(404, 'not_found'));
    server.on('GET /api/dialogs/d-x/messages', () => fail(404, 'not_found'));
  });
});
