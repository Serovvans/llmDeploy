/**
 * Чистые правила веб-видов «Кода» (концепция §5.13.19–5.13.22): что в HTML проверяется как JavaScript,
 * текст вопроса «Исправить с помощником», строки файлов для окна выгрузки.
 */
import { texts } from '../../texts';

const r = texts.workshop.web.route;

/** Строк в файле — как их показывает редактор; пустой файл — 0. */
export function lineCount(text: string): number {
  if (text === '') {
    return 0;
  }
  const lines = text.split(/\r\n|\r|\n/);
  return lines.at(-1) === '' ? lines.length - 1 : lines.length;
}

export type CatalogType = 'css' | 'html' | 'js';

/** Тип кастомизации Каталога — по расширению файла (§5.13.22). */
export function catalogTypeOf(path: string): CatalogType {
  const extension = path.slice(path.lastIndexOf('.') + 1).toLowerCase();
  return extension === 'css' ? 'css' : extension === 'html' ? 'html' : 'js';
}

export type FileLanguage = 'csharp' | 'javascript' | 'html' | 'css' | 'plaintext';

/** Язык редактора по расширению (§5.13.19). */
export function languageOf(path: string): FileLanguage {
  const extension = path.slice(path.lastIndexOf('.') + 1).toLowerCase();
  switch (extension) {
    case 'cs':
      return 'csharp';
    case 'js':
      return 'javascript';
    case 'html':
      return 'html';
    case 'css':
      return 'css';
    default:
      return 'plaintext';
  }
}

/** Что в HTML проверяется как JavaScript (§5.13.19): код внутри `<script>` и значения `data-bind`. */
export interface HtmlCode {
  /**
   * Текст той же длины, что исходный: вне тел скриптов JavaScript — пробелы (переводы строк сохранены),
   * вместо `<` закрывающего `</script>` — `;`, чтобы скрипты не сливались. Смещения совпадают с исходными.
   */
  script: string;
  /** Тела скриптов JavaScript: `[начало, конец)` в исходном тексте. */
  scripts: [number, number][];
  /** Значения `data-bind`, каждое как `({…\n});`; `map[i]` — смещение знака `i` в исходном тексте. */
  bindings: { text: string; map: number[] };
}

const JS_TYPES = new Set(['', 'text/javascript', 'application/javascript', 'module', 'text/ecmascript', 'application/ecmascript']);
const ENTITIES: Record<string, string> = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#39': "'", '#34': '"' };

function insideAny(ranges: readonly [number, number][], offset: number): boolean {
  return ranges.some(([start, end]) => offset >= start && offset < end);
}

export function extractHtml(text: string): HtmlCode {
  const comments: [number, number][] = [];
  for (const match of text.matchAll(/<!--[\s\S]*?(?:-->|$)/g)) {
    comments.push([match.index, match.index + match[0].length]);
  }
  const scripts: [number, number][] = [];
  const closers: number[] = [];
  for (const match of text.matchAll(/<script\b([^>]*)>([\s\S]*?)(<\/script\s*>|$)/gi)) {
    if (insideAny(comments, match.index)) {
      continue;
    }
    const type = /\btype\s*=\s*["']?([^"'\s>]*)/i.exec(match[1] ?? '')?.[1]?.toLowerCase() ?? '';
    if (!JS_TYPES.has(type)) {
      // Шаблон Knockout (`text/html`) и прочие нестандартные типы — разметка, а не код.
      continue;
    }
    const start = match.index + match[0].length - (match[2] ?? '').length - (match[3] ?? '').length;
    const end = start + (match[2] ?? '').length;
    scripts.push([start, end]);
    if (match[3]) {
      closers.push(end);
    }
  }
  // `split('')` идёт по единицам UTF-16 — как смещения Monaco и TypeScript.
  const blank = text.replace(/[^\r\n]/g, ' ').split('');
  for (const [start, end] of scripts) {
    for (let index = start; index < end; index += 1) {
      blank[index] = text[index] ?? ' ';
    }
  }
  for (const closer of closers) {
    blank[closer] = ';';
  }

  let bindings = '';
  const map: number[] = [];
  const push = (part: string, offset: number) => {
    for (let index = 0; index < part.length; index += 1) {
      bindings += part[index];
      map.push(offset);
    }
  };
  for (const match of text.matchAll(/\sdata-bind\s*=\s*(?:"([^"]*)"|'([^']*)')/gi)) {
    if (insideAny(comments, match.index) || insideAny(scripts, match.index)) {
      continue;
    }
    const value = match[1] ?? match[2] ?? '';
    const valueStart = match.index + match[0].length - value.length - 1;
    push('({', valueStart);
    for (let index = 0; index < value.length; ) {
      const entity = /^&(amp|lt|gt|quot|apos|#39|#34);/.exec(value.slice(index, index + 6));
      if (entity) {
        push(ENTITIES[entity[1] ?? ''] ?? '', valueStart + index);
        index += entity[0].length;
      } else {
        push(value[index] ?? '', valueStart + index);
        index += 1;
      }
    }
    push('\n});\n', valueStart + value.length);
  }
  return { script: blank.join(''), scripts, bindings: { text: bindings, map } };
}

/** Строка и столбец (с 1) по смещению — как у Monaco. */
export function positionAt(text: string, offset: number): { line: number; column: number } {
  const before = text.slice(0, Math.max(0, Math.min(offset, text.length)));
  const lines = before.split('\n');
  return { line: lines.length, column: (lines.at(-1)?.length ?? 0) + 1 };
}

/** Сообщение проверки: русское — как есть, без русского текста — «Ошибка синтаксиса» (Д-67). */
export function syntaxMessage(message: string): string {
  return /[а-яё]/i.test(message) ? message : r.syntaxError;
}

export interface SyntaxMessage {
  path: string;
  line: number;
  column: number;
  endLine: number;
  endColumn: number;
  message: string;
}

/** Вопрос «Исправить с помощником» (§5.13.20): до десяти сообщений, дальше «и ещё ‹N›». */
export function fixQuestion(kind: 'syntax' | 'preview', items: readonly { path: string | null; line: number | null; message: string }[]): string {
  const shown = items.slice(0, 10).map((item) => r.fixLine(item.path ?? texts.workshop.web.pane.placeUnknown, item.line, item.message));
  const more = items.length > 10 ? [r.fixMore(items.length - 10)] : [];
  return [kind === 'syntax' ? r.fixSyntax : r.fixPreview, ...shown, ...more].join('\n');
}
