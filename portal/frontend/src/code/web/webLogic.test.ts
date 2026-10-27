import { describe, expect, it } from 'vitest';

import { texts } from '../../texts';
import { appendMessage, EMPTY_LOG, errorCount, errorName, type FrameMessage, logResult, MAX_TEXT, notInserted, parseFrameMessage, previewFix } from './preview';
import { sqlParameters, stripQuotes } from './sqlParams';
import { extractHtml, fixQuestion, languageOf } from './webRules';

const LIMITS = { log_max_entries: 2, errors_max: 2 };
const PATHS = new Set(['plugin.js', 'widget.html']);

describe('сообщения фрейма предпросмотра — недоверенные данные', () => {
  it('неизвестный тип и поля не своих типов отбрасываются', () => {
    expect(parseFrameMessage('ready', PATHS)).toBeNull();
    expect(parseFrameMessage({ type: 'init' }, PATHS)).toBeNull();
    expect(parseFrameMessage({ type: 'log', fn: 'getLayer', args: [1], known: true, count: null }, PATHS)).toBeNull();
    expect(parseFrameMessage({ type: 'log', fn: 'getLayer', args: [], known: 'yes', count: null }, PATHS)).toBeNull();
    expect(parseFrameMessage({ type: 'error', file: null, line: 0, column: null, message: 'x' }, PATHS)).toBeNull();
    expect(parseFrameMessage({ type: 'blocked', url: 'https://example.com' }, PATHS)).toBeNull();
  });

  it('ошибка с файлом не из проекта и запрет с директивой не из списка отбрасываются целиком (§7.5.17)', () => {
    const error = { type: 'error', line: 3, column: 1, message: 'Uncaught TypeError: x' };
    expect(parseFrameMessage({ ...error, file: 'plugin.js' }, PATHS)).toMatchObject({ type: 'error', file: 'plugin.js' });
    expect(parseFrameMessage({ ...error, file: null }, PATHS)).toMatchObject({ type: 'error', file: null });
    expect(parseFrameMessage({ ...error, file: 'Иванов Иван.js' }, PATHS)).toBeNull();
    expect(parseFrameMessage({ ...error, file: 'plugin.js ' }, PATHS)).toBeNull();
    const blocked = { type: 'blocked', url: 'https://api.example' };
    expect(parseFrameMessage({ ...blocked, directive: 'connect-src' }, PATHS)).toMatchObject({ directive: 'connect-src' });
    // Имя по шаблону [a-z-], но не из списка, — тоже канал для значений строк базы.
    for (const directive of ['', 'a', 'abc', 'connect-srcx', 'cadastre-ivanov', 'frame-ancestors', 'sandbox', 'Connect-src', 'connect-src 50:01', 'img-src;']) {
      expect(parseFrameMessage({ ...blocked, directive }, PATHS)).toBeNull();
    }
  });

  it('длинные строки обрезаются, аргументов — не больше двадцати', () => {
    const long = 'x'.repeat(MAX_TEXT + 10);
    const parsed = parseFrameMessage({ type: 'log', fn: long, args: Array.from({ length: 30 }, () => long), known: false, count: 3 }, PATHS);
    expect(parsed).toMatchObject({ type: 'log', known: false, count: 3 });
    if (parsed?.type !== 'log') {
      throw new Error('ожидалась запись журнала');
    }
    expect(parsed.fn).toHaveLength(MAX_TEXT);
    expect(parsed.args).toHaveLength(20);
    expect(parsed.args.every((arg) => arg.length === MAX_TEXT)).toBe(true);
  });

  it('журнал и ошибки — в пределах; запрет картинки ошибкой кода не считается', () => {
    const log: FrameMessage = { type: 'log', fn: 'showAlert', args: ['a'], known: true, count: null };
    let state = appendMessage(EMPTY_LOG, log, LIMITS);
    state = appendMessage(state, log, LIMITS);
    state = appendMessage(state, log, LIMITS);
    expect(state.entries).toHaveLength(2);
    expect(state.overflow).toBe(true);

    state = appendMessage(state, { type: 'blocked', url: 'https://tiles.example/1.png', directive: 'img-src' }, LIMITS);
    state = appendMessage(state, { type: 'error', file: 'widget.html', line: 3, column: 5, message: 'boom' }, LIMITS);
    state = appendMessage(state, { type: 'blocked', url: 'https://api.example', directive: 'connect-src' }, LIMITS);
    expect(state.problems.map((item) => item.kind)).toEqual(['image', 'error']);
    expect(errorCount(state)).toBe(1);
  });

  it('слой из образца базы: в вопрос помощнику — только файл, строка, тип ошибки и директива (ПД §5.8)', () => {
    const big = { log_max_entries: 10, errors_max: 10 };
    let state = appendMessage(
      EMPTY_LOG,
      { type: 'error', file: 'plugin.js', line: 4, column: 9, message: 'Uncaught SyntaxError: Unexpected token \'И\', "Иванов Иван Иванович" is not valid JSON' },
      big,
    );
    state = appendMessage(state, { type: 'error', file: null, line: null, column: null, message: 'Uncaught (in promise) TypeError: Иванов' }, big);
    state = appendMessage(state, { type: 'error', file: 'plugin.js', line: 7, column: 1, message: 'Uncaught Иванов Иван' }, big);
    state = appendMessage(state, { type: 'blocked', url: 'https://x.example/?n=50:01:0000000:123', directive: 'connect-src' }, big);
    state = appendMessage(state, { type: 'blocked', url: 'https://x.example/Иванов.png', directive: 'img-src' }, big);

    const sample = previewFix(state.problems, 'sample');
    expect(sample).toEqual({
      layer: 'sample',
      problems: [
        { kind: 'error', path: 'plugin.js', line: 4, type: 'SyntaxError' },
        { kind: 'error', path: null, line: null, type: 'TypeError' },
        { kind: 'error', path: 'plugin.js', line: 7, type: 'Error' },
        { kind: 'network', path: null, line: null, directive: 'connect-src' },
      ],
    });
    expect(JSON.stringify(sample)).not.toMatch(/Иванов|50:01|x\.example/);

    const invented = previewFix(state.problems, 'invented');
    expect(invented.layer).toBe('invented');
    expect(invented.problems[0]).toMatchObject({ type: 'SyntaxError', text: expect.stringContaining('Иванов Иван Иванович') as unknown });
    expect(invented.problems[3]).toEqual({ kind: 'network', path: null, line: null, directive: 'connect-src', text: 'https://x.example/?n=50:01:0000000:123' });
  });

  it('тип ошибки — только из закрытого списка контракта, иначе Error', () => {
    expect(errorName('Uncaught DOMException: x')).toBe('DOMException');
    expect(errorName('Uncaught (in promise) RangeError: x')).toBe('RangeError');
    expect(errorName('Uncaught ИвановError: x')).toBe('Error');
    expect(errorName('Uncaught CadastreError: 50:01')).toBe('Error');
    expect(errorName('Script error.')).toBe('Error');
  });

  it('«что сделано» — по функции; слой из образца называет таблицу и число объектов', () => {
    const r = texts.workshop.web.pane.results;
    expect(logResult({ fn: 'nope', args: [], known: false, count: null }, { table: null, rows: 0 })).toBe(r.unknown);
    expect(logResult({ fn: 'getLayer', args: [], known: true, count: 7 }, { table: 'cadastre.parcels', rows: 50 })).toBe(
      r.layerSample('cadastre.parcels', 7),
    );
    expect(logResult({ fn: 'generateReport', args: [], known: true, count: null }, { table: null, rows: 0 })).toBe(r.notRun);
  });

  it('файлы Каталога, не вставленные для типа клиента', () => {
    const files = [
      { path: 'a.css', catalog: { insertion: 'head_end' as const, clients: ['desktop' as const] } },
      { path: 'b.js', catalog: { insertion: 'body_end' as const, clients: ['desktop' as const, 'smartphone' as const] } },
    ];
    expect(notInserted(files, 'smartphone')).toEqual(['a.css']);
  });
});

describe('параметры «Проверить запрос» — правило сервера', () => {
  it.each<[string, string[]]>([
    ['SELECT * FROM t WHERE id = @id AND s = @status OR id = @id', ['@id', '@status']],
    ['WHERE (@x) AND y=@y', ['@x', '@y']],
    ['SELECT @ x, abs(x), a@b, 1@2, <@c', []],
    ["SELECT '@no', \"@quoted\" -- @comment\n/* @block */ FROM t WHERE a = $1 AND b = $2 AND c = $1", ['$1', '$2']],
    ['SELECT $body$ @inside $1 $body$, $$ @x $$, x$1', []],
    ["WHERE name = 'O''Brien' AND id = @id", ['@id']],
  ])('%s', (sql, expected) => {
    expect(sqlParameters(sql)).toEqual(expected);
  });

  it('кавычки вокруг выделения убираются, только парные', () => {
    expect(stripQuotes(' "SELECT 1" ')).toBe('SELECT 1');
    expect(stripQuotes('@"SELECT 1"')).toBe('SELECT 1');
    expect(stripQuotes("'SELECT 1'")).toBe('SELECT 1');
    expect(stripQuotes('`SELECT 1`')).toBe('SELECT 1');
    expect(stripQuotes('"SELECT 1\'')).toBe('"SELECT 1\'');
  });
});

describe('JavaScript внутри HTML', () => {
  it('смещения кода совпадают с исходными; разметка и шаблоны Knockout — пробелы', () => {
    const html = '<div>\n<script>let a = 1;</script>\n<script type="text/html">x</script><script>b()</script>';
    const { script, scripts } = extractHtml(html);
    expect(script).toHaveLength(html.length);
    expect(scripts).toHaveLength(2);
    const [first] = scripts;
    expect(first && script.slice(first[0], first[1])).toBe('let a = 1;');
    expect(script.indexOf('x')).toBe(-1);
    expect(script.split('\n')).toHaveLength(html.split('\n').length);
  });

  it('значение data-bind — выражение, знаки которого указывают в исходный текст', () => {
    const html = '<input data-bind="value: name, click: save">';
    const { bindings } = extractHtml(html);
    const index = bindings.text.indexOf('save');
    expect(html.slice(bindings.map[index], (bindings.map[index] ?? 0) + 4)).toBe('save');
  });

  it('язык редактора — по расширению', () => {
    expect(['a.cs', 'b.JS', 'c.html', 'd.css', 'e.txt'].map(languageOf)).toEqual(['csharp', 'javascript', 'html', 'css', 'plaintext']);
  });

  it('вопрос «Исправить с помощником» — не больше десяти строк и «и ещё N»', () => {
    const items = Array.from({ length: 12 }, (_, i) => ({ path: 'widget.html', line: i + 1, message: `ошибка ${i}` }));
    const text = fixQuestion(items).split('\n');
    expect(text).toHaveLength(12);
    expect(text.at(-1)).toBe(texts.workshop.web.route.fixMore(2));
  });
});
