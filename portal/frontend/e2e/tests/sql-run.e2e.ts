/**
 * Критерии 16 и 17 (docs/portal-design.md §10): компоновка экрана SQL и проверка запросов на
 * тестовых данных. Песочница на стенде настоящая; тестовые данные и исправление запроса
 * заглушка берёт из пометок в тексте схемы (docs/portal-api.md §12.2).
 */
import type { Locator, Page } from '@playwright/test';

import { uniqueSuffix } from '../support/accounts';
import { expect, test, type Member } from '../support/fixtures';
import { must } from '../support/must';
import { askSql, createDialog, eventsOf, expectError, saveSqlSchema, stubReply } from '../support/portal';
import { fetchPlain, send } from '../support/ui';

const SQL_WRITE = /Опишите словами, что нужно получить/;
const TABLE = 'CREATE TABLE parcels (id int PRIMARY KEY, cadastral_number text, area numeric)';
const ROWS = "INSERT INTO parcels VALUES (1, '77:01:0004012:345', 120.5), (2, '77:01:0004012:346', 80)";
const DISCLAIMER = 'Данные придуманы моделью.';

interface RunBlock {
  status: string;
  reason: string | null;
  fixture: { table: string; columns: string[]; rows: (string | null)[][]; truncated: boolean }[] | null;
  result: { columns: string[]; rows: (string | null)[][]; truncated: boolean } | null;
  affected_rows: number | null;
  error: string | null;
  fixed_sql: string | null;
}

/** Текст схемы: описание таблицы и ответы заглушки на запросы тестовых данных и исправления. */
function schemaText(options: { data?: boolean; fix?: string } = {}): string {
  const lines = [`${TABLE};`];
  if (options.data ?? true) {
    lines.push(`-- [[stub:sql-data]]${JSON.stringify({ statements: [TABLE, ROWS] })}[[/stub:sql-data]]`);
  }
  if (options.fix) {
    lines.push(`-- [[stub:sql-fix]]\`\`\`sql\n${options.fix}\n\`\`\`[[/stub:sql-fix]]`);
  }
  return lines.join('\n');
}

function sqlBlocks(...queries: string[]): string {
  return stubReply(queries.map((query, index) => `Запрос ${index + 1}:\n\`\`\`sql\n${query}\n\`\`\``).join('\n'));
}

/** Задаёт вопрос с проверкой и возвращает диалог, итог проверки по блокам и все события `sql_run`. */
async function checked(member: Member, schemaId: string, ...queries: string[]) {
  const dialogId = await createDialog(member.api, 'sql');
  const events = await askSql(member.api, dialogId, `Напиши запрос ${sqlBlocks(...queries)}`, { schemaId, testRun: true });
  expect(events.at(-1)?.event).toBe('done');
  const runs = eventsOf(events, 'sql_run');
  const blocks: RunBlock[] = must(runs.at(-1), 'событие sql_run').data.blocks;
  return { dialogId, blocks, runs };
}

async function newSchema(member: Member, content: string): Promise<string> {
  return saveSqlSchema(member.api, `Кадастр ${uniqueSuffix()}`, content);
}

/** Сохранённый диалог открыт: ответ и его действия на экране. */
async function openDialog(page: Page, dialogId: string): Promise<void> {
  await page.goto(`/sql/${dialogId}`);
  await expect(page.getByRole('button', { name: 'Ответить заново' })).toBeVisible();
}

test.describe('Проверка SQL на тестовых данных', () => {
  test('успех: под блоком — статус, тестовые данные и результат не больше 50 строк; имя таблицы со схемой проверяется', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const schemaId = await newSchema(member, schemaText());
    const { dialogId, blocks, runs } = await checked(
      member,
      schemaId,
      'SELECT id, area FROM parcels WHERE area > 100;',
      'SELECT g FROM generate_series(1, 60) AS g;',
      'SELECT cadastral_number FROM cadastre.parcels ORDER BY id;',
    );

    expect(blocks.map((block) => block.status)).toEqual(['passed', 'passed', 'passed']);
    expect(blocks[0]).toMatchObject({
      reason: null,
      fixture: [
        {
          table: 'parcels',
          columns: ['id', 'cadastral_number', 'area'],
          rows: [
            ['1', '77:01:0004012:345', '120.5'],
            ['2', '77:01:0004012:346', '80'],
          ],
          truncated: false,
        },
      ],
      result: { columns: ['id', 'area'], rows: [['1', '120.5']], truncated: false },
      affected_rows: null,
      error: null,
      fixed_sql: null,
    });
    expect(must(blocks[1]?.result, 'результат второго блока')).toMatchObject({ truncated: true });
    expect(blocks[1]?.result?.rows).toHaveLength(50);
    expect(blocks[2]?.result?.rows).toEqual([['77:01:0004012:345'], ['77:01:0004012:346']]);

    // Ход проверки виден только в событиях; в сохранённом ответе — итог.
    const seen = runs.flatMap((run) => (run.data.blocks as RunBlock[]).map((block) => block.status));
    expect(seen).toContain('running');
    const saved = await (await member.api.get(`/api/dialogs/${dialogId}/messages`)).json();
    const answer = saved.items.find((message: { role: string }) => message.role === 'assistant');
    expect(answer.sql_run.blocks.map((block: RunBlock) => block.status)).toEqual(['passed', 'passed', 'passed']);

    const page = await pageAs(member, '/sql');
    await openDialog(page, dialogId);
    await expect(page.getByText('Запрос выполнился на тестовых данных')).toHaveCount(3);
    await expect(page.getByText(DISCLAIMER).first()).toBeVisible();
    await expect(page.getByText('Результат · 1 строка')).toBeVisible();
    await expect(page.getByText('Результат · 50 строк')).toBeVisible();
    await expect(page.getByText('Показаны первые 50 строк')).toBeVisible();
    // Тестовые данные свёрнуты; раскрываются таблицей.
    const fixture = page.getByRole('button', { name: 'Тестовые данные · 1 таблица' }).first();
    await expect(fixture).toHaveAttribute('aria-expanded', 'false');
    await fixture.click();
    await expect(page.getByRole('region', { name: 'Тестовая таблица parcels' })).toContainText('77:01:0004012:346');
  });

  test('запрос с ошибкой исполнения исправляется и помечается «исправлено»', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const schemaId = await newSchema(member, schemaText({ fix: 'SELECT id, area FROM parcels ORDER BY id' }));
    const { dialogId, blocks } = await checked(member, schemaId, 'SELECT id, aera FROM parcels;');

    expect(blocks).toHaveLength(1);
    expect(blocks[0]).toMatchObject({
      status: 'fixed',
      fixed_sql: 'SELECT id, area FROM parcels ORDER BY id',
      result: {
        rows: [
          ['1', '120.5'],
          ['2', '80'],
        ],
      },
    });
    expect(blocks[0]?.error).toContain('aera');
    expect(blocks[0]?.fixture).not.toBeNull();

    const page = await pageAs(member, '/sql');
    await openDialog(page, dialogId);
    await expect(page.getByText('Исправлено: запрос из ответа не выполнился, ниже — исправленный')).toBeVisible();
    await expect(page.getByText('Ошибка исходного запроса')).toBeVisible();
    await expect(page.locator('main')).toContainText('column "aera" does not exist');
    await expect(page.locator('pre').filter({ hasText: 'SELECT id, area FROM parcels ORDER BY id' })).toBeVisible();
    // Текст ответа не переписан: исходный запрос остался в нём.
    await expect(page.locator('pre').filter({ hasText: 'SELECT id, aera FROM parcels;' })).toBeVisible();
  });

  test('запрос, который не удалось исправить, получает «ошибка» с сообщением базы', async ({ newMember, pageAs }) => {
    const member = await newMember();
    // Без пометки исправления заглушка возвращает тот же запрос: обе попытки неудачны.
    const schemaId = await newSchema(member, schemaText());
    const { dialogId, blocks } = await checked(member, schemaId, 'SELECT id, aera FROM parcels;');

    expect(blocks[0]).toMatchObject({ status: 'failed', result: null, fixed_sql: null });
    expect(blocks[0]?.error).toContain('aera');
    // Данные, на которых запрос не выполнился, отдаются и при ошибке (docs/portal-api.md §7.4).
    expect(blocks[0]?.fixture?.[0]?.table).toBe('parcels');

    const page = await pageAs(member, '/sql');
    await openDialog(page, dialogId);
    await expect(page.getByText('Запрос не выполнился на тестовых данных')).toBeVisible();
    await expect(page.getByText('Сообщение базы данных')).toBeVisible();
    await expect(page.locator('main')).toContainText('column "aera" does not exist');
  });

  test('UPDATE и DELETE выполняются и не оставляют изменений; DDL, DROP, TRUNCATE и несколько операторов не проверяются', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const schemaId = await newSchema(member, schemaText());

    const changing = await checked(
      member,
      schemaId,
      'UPDATE parcels SET area = 0;',
      'DELETE FROM parcels WHERE id = 1;',
      'SELECT count(*) AS total, sum(area) AS area FROM parcels;',
    );
    expect(changing.blocks.map((block) => [block.status, block.affected_rows])).toEqual([
      ['passed', 2],
      ['passed', 1],
      ['passed', null],
    ]);
    // Третий блок видит данные нетронутыми: изменения первых двух отменены.
    expect(changing.blocks[2]?.result?.rows).toEqual([['2', '200.5']]);

    const skipped = await checked(
      member,
      schemaId,
      'DROP TABLE parcels;',
      'TRUNCATE parcels;',
      'CREATE TABLE owners (id int);',
    );
    expect(skipped.blocks.map((block) => [block.status, block.reason, block.fixture])).toEqual([
      ['not_checked', 'statement_not_supported', null],
      ['not_checked', 'statement_not_supported', null],
      ['not_checked', 'statement_not_supported', null],
    ]);
    const several = await checked(member, schemaId, 'SELECT 1; SELECT 2;');
    expect(several.blocks.map((block) => [block.status, block.reason])).toEqual([['not_checked', 'multiple_statements']]);

    // Схема, по которой заглушка тестовых данных не даёт.
    const bare = await checked(member, await newSchema(member, schemaText({ data: false })), 'SELECT id FROM parcels;');
    expect(bare.blocks.map((block) => [block.status, block.reason])).toEqual([['not_checked', 'test_data_failed']]);

    const page = await pageAs(member, '/sql');
    await openDialog(page, changing.dialogId);
    await expect(page.getByText('Затронуто строк: 2. Изменения отменены — проверка ничего не сохраняет')).toBeVisible();
    await expect(page.getByText('Затронуто строк: 1. Изменения отменены — проверка ничего не сохраняет')).toBeVisible();
    await openDialog(page, skipped.dialogId);
    await expect(
      // Точное совпадение: объявление для экранного чтения повторяет эти слова одной строкой.
      page.getByText(
        'Не проверялось: на тестовых данных выполняются только запросы, которые читают или меняют строки таблиц',
        { exact: true },
      ),
    ).toHaveCount(3);
    await expect(page.getByText(DISCLAIMER)).toHaveCount(0);
    await openDialog(page, several.dialogId);
    await expect(page.getByText('Не проверялось: в блоке несколько запросов, а проверяется по одному').first()).toBeVisible();
  });

  test('враждебный запрос из ответа модели заканчивается отказом, а не чтением файлов, зависанием или переполнением', async ({
    newMember,
  }) => {
    test.setTimeout(120_000);
    const member = await newMember();
    const schemaId = await newSchema(member, schemaText());
    // Каждый запрос падает трижды: сам и после двух «исправлений» заглушки, которая возвращает его же.
    const { blocks } = await checked(
      member,
      schemaId,
      "SELECT pg_read_file('/etc/passwd');",
      'SELECT pg_sleep(60);',
      "SELECT repeat('x', 100000) FROM generate_series(1, 100000);",
    );
    expect(blocks.map((block) => [block.status, block.result])).toEqual([
      ['failed', null],
      ['failed', null],
      ['failed', null],
    ]);
    expect(blocks[0]?.error).toMatch(/permission denied/);
    expect(blocks[1]?.error).toMatch(/statement timeout/);
    expect(blocks[2]?.error).toBe('result is too large');
    // Песочница после них работает: обычная проверка проходит.
    const after = await checked(member, schemaId, 'SELECT count(*) FROM parcels;');
    expect(after.blocks[0]).toMatchObject({ status: 'passed', result: { rows: [['2']] } });
  });

  test('без схемы переключатель неактивен; со схемой проверка включается и уходит вместе с вопросом', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const schemaName = `Кадастр ${uniqueSuffix()}`;
    const schemaId = await saveSqlSchema(member.api, schemaName, schemaText());

    // Сервер проверку без схемы не принимает.
    const dialogId = await createDialog(member.api, 'sql');
    const refused = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: 'Напиши запрос', action: 'write', dialect: 'postgres', schema_id: null, test_run: true },
    });
    await expectError(refused, 422, 'validation_error');
    expect((await refused.json()).error.fields).toEqual([expect.objectContaining({ field: 'test_run' })]);

    const page = await pageAs(member, '/sql');
    const toggle = page.getByRole('switch', { name: 'Проверять на тестовых данных' });
    await expect(toggle).toHaveAttribute('aria-disabled', 'true');
    await expect(toggle).toHaveAttribute('aria-checked', 'false');
    await expect(page.getByText('Чтобы проверять запросы, выберите схему базы')).toBeAttached();

    await page.getByRole('button', { name: 'Схема базы' }).click();
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: schemaName }).click();
    await expect(toggle).not.toHaveAttribute('aria-disabled', 'true');
    // По умолчанию проверка выключена.
    await expect(toggle).not.toBeChecked();
    await page.getByText('Проверять на тестовых данных', { exact: true }).click();
    await expect(toggle).toBeChecked();

    const request = page.waitForRequest((candidate) => /\/api\/dialogs\/[^/]+\/messages$/.test(candidate.url()));
    await send(page, `Участки больше ста метров ${sqlBlocks('SELECT id FROM parcels WHERE area > 100;')}`, SQL_WRITE);
    expect((await request).postDataJSON()).toMatchObject({ schema_id: schemaId, test_run: true });
    await expect(page.getByText('Запрос выполнился на тестовых данных')).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText('Результат · 1 строка')).toBeVisible();

    // Выбор запоминается; без схемы переключатель снова неактивен и показан выключенным.
    await page.reload();
    await expect(toggle).toBeChecked();
    await page.getByRole('button', { name: 'Схема базы' }).click();
    await page.locator('[data-tid~="MenuItem__root"]').filter({ hasText: 'Без схемы' }).click();
    await expect(toggle).toHaveAttribute('aria-disabled', 'true');
  });

  test('при выключенном флаге проверки переключателя нет', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, '/chat');
    // Флаг портала на стенде включён: настройки для интерфейса подменяются на пути к браузеру.
    await page.route('**/api/config', async (route) => {
      const response = await fetchPlain(route);
      const config = await response.json();
      config.sql.test_run.enabled = false;
      await route.fulfill({ response, json: config });
    });
    await page.goto('/sql');
    await expect(page.getByRole('button', { name: 'Диалект' })).toContainText('PostgreSQL + PostGIS');
    await expect(page.getByText('Портал не подключается к вашим базам и не выполняет запросы', { exact: true })).toBeVisible();
    await expect(page.getByRole('switch', { name: 'Проверять на тестовых данных' })).toHaveCount(0);
  });
});

test.describe('Компоновка экрана SQL', () => {
  for (const colorScheme of ['light', 'dark'] as const) {
    test(`настройки — в строке заголовка справа, выбор действия — под полем ввода (${colorScheme})`, async ({
      newMember,
      pageAs,
    }) => {
      const member = await newMember();
      const page = await pageAs(member, '/chat');
      await page.emulateMedia({ colorScheme });
      await page.goto('/sql');

      const box = async (locator: Locator) => must(await locator.boundingBox(), 'рамка элемента');
      const settings = [
        page.getByRole('button', { name: 'Диалект' }),
        page.getByRole('button', { name: 'Схема базы' }),
        page.getByRole('button', { name: 'Мои схемы' }),
        page.getByRole('switch', { name: 'Проверять на тестовых данных' }),
      ];
      // Настройки портала загружены: до этого списка диалектов нет, и строка ещё перестраивается.
      await expect(must(settings[0], 'диалект')).toContainText('PostgreSQL + PostGIS');
      for (const item of settings) {
        await expect(item).toBeVisible();
      }
      const heading = await box(page.getByRole('heading', { level: 1, name: 'SQL-помощник' }));
      let previousRight = heading.x + heading.width;
      for (const item of settings) {
        const rect = await box(item);
        // Та же строка, что у заголовка, правее него и в порядке концепции.
        expect(Math.abs(rect.y + rect.height / 2 - (heading.y + heading.height / 2))).toBeLessThan(heading.height);
        expect(rect.x).toBeGreaterThanOrEqual(previousRight - 1);
        previousRight = rect.x + rect.width;
      }
      const viewport = must(page.viewportSize(), 'размер окна');
      expect((await box(must(settings.at(-1), 'переключатель'))).x).toBeGreaterThan(viewport.width / 2);

      const field = await box(page.getByRole('textbox', { name: SQL_WRITE }));
      for (const action of ['Написать запрос', 'Объяснить запрос', 'Найти ошибку', 'Оптимизировать']) {
        const rect = await box(page.getByRole('switch', { name: action }));
        expect(rect.y).toBeGreaterThanOrEqual(field.y + field.height - 1);
      }

      // Тема применена ко всему экрану.
      const background = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      const [red] = must(/\d+/.exec(background), 'цвет фона').map(Number);
      expect(must(red, 'красная составляющая') < 128).toBe(colorScheme === 'dark');
    });
  }

  test('экран помощника CoGIS (раздел «Код» выключен) не изменился: настроек SQL в заголовке нет, действия — отдельной строкой над лентой', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const page = await pageAs(member, '/chat');
    // С 0.9 прежний экран открывается только при выключенном разделе «Код»: флаг подменяется на пути
    // к браузеру (настоящий флаг портала проверяет code-stand.e2e.ts).
    await page.route('**/api/config', async (route) => {
      const response = await fetchPlain(route);
      const config = await response.json();
      config.code.enabled = false;
      await route.fulfill({ response, json: config });
    });
    await page.goto('/cogis');
    await expect(page.getByRole('heading', { level: 1, name: 'Помощник CoGIS' })).toBeVisible();
    for (const name of ['Диалект', 'Схема базы', 'Мои схемы']) {
      await expect(page.getByRole('button', { name })).toHaveCount(0);
    }
    await expect(page.getByRole('switch', { name: 'Проверять на тестовых данных' })).toHaveCount(0);
    const heading = must(await page.getByRole('heading', { level: 1 }).boundingBox(), 'заголовок');
    const field = must(await page.getByRole('textbox').first().boundingBox(), 'поле ввода');
    for (const name of ['Написать плагин', 'Объяснить код', 'Разобрать ошибку сборки']) {
      const action = must(await page.getByRole('switch', { name }).boundingBox(), 'действие');
      expect(action.y).toBeGreaterThan(heading.y + heading.height);
      expect(action.y + action.height).toBeLessThan(field.y);
    }
    await expect(page.getByRole('main').getByRole('switch')).toHaveCount(3);
  });
});
