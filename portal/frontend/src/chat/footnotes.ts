/**
 * Что считается сноской (контракт §5.5): подстрока `[n]` вне кода, `n` — без ведущих нулей.
 * Правило построчное и повторяет серверное буквально (`portal/backend/src/portal/dialogs/footnotes.py`),
 * иначе кнопки-сноски разошлись бы с сохранёнными источниками:
 * - ограждённый блок открывает строка с тремя и более обратными апострофами или тильдами (до трёх
 *   пробелов отступа), закрывает строка из тех же символов не короче открывающей; незакрытый — до конца;
 * - любая непустая строка с отступом в четыре пробела или табуляцией — код;
 * - встроенный код — между сериями обратных апострофов одной длины, в том числе через перевод строки;
 *   серия без пары — обычный текст.
 *
 * Известное различие: Python `strip()` и JavaScript `trim()` по-разному трактуют экзотические пробелы
 * (например, U+FEFF, U+001C) в строке, закрывающей блок кода. Расхождение даёт только карточку без
 * кнопки в тексте; лишнюю кнопку исключает проверка источника в `SourceMark`.
 */

const FENCE = /^ {0,3}(`{3,}|~{3,})/;
const INDENTED = /^(?: {4}|\t)/;
const BACKTICKS = /`+/g;
const FOOTNOTE = /\[([1-9][0-9]*)\]/g;

/** Символы текста вне кода и место каждого в исходном тексте. */
interface Prose {
  text: string;
  offsets: number[];
}

function outsideCodeBlocks(text: string): Prose {
  let prose = '';
  const offsets: number[] = [];
  let fence: string | null = null;
  let lineStart = 0;
  const lines = text.split('\n');
  lines.forEach((line, index) => {
    const opened = FENCE.exec(line)?.[1];
    let keep = false;
    if (fence !== null) {
      const closes = opened !== undefined && opened[0] === fence[0] && opened.length >= fence.length;
      if (closes && line.trim().replaceAll(fence.charAt(0), '') === '') {
        fence = null;
      }
    } else if (opened !== undefined) {
      fence = opened;
    } else if (!(INDENTED.test(line) && line.trim())) {
      keep = true;
    }
    if (keep) {
      for (let i = 0; i < line.length; i += 1) {
        offsets.push(lineStart + i);
      }
      prose += line;
    }
    if (index < lines.length - 1) {
      offsets.push(lineStart + line.length);
      prose += '\n';
    }
    lineStart += line.length + 1;
  });
  return { text: prose, offsets };
}

function outsideInlineCode({ text, offsets }: Prose): Prose {
  const runs = [...text.matchAll(BACKTICKS)].map((match) => ({ start: match.index, end: match.index + match[0].length }));
  let kept = '';
  const keptOffsets: number[] = [];
  const keep = (from: number, to: number) => {
    kept += text.slice(from, to);
    keptOffsets.push(...offsets.slice(from, to));
  };
  let position = 0;
  let next = 0;
  while (next < runs.length) {
    const opening = runs[next] as { start: number; end: number };
    const closingIndex = runs.findIndex(
      (run, index) => index > next && run.end - run.start === opening.end - opening.start,
    );
    if (closingIndex < 0) {
      // Серия без пары — обычные символы, а не начало кода.
      keep(position, opening.end);
      position = opening.end;
      next += 1;
      continue;
    }
    keep(position, opening.start);
    position = (runs[closingIndex] as { end: number }).end;
    next = closingIndex + 1;
  }
  keep(position, text.length);
  return { text: kept, offsets: keptOffsets };
}

interface Footnote {
  n: number;
  /** Место пометки в исходном тексте; `null`, если её символы разделены вырезанным кодом. */
  start: number | null;
  length: number;
}

function findFootnotes(text: string): Footnote[] {
  const prose = outsideInlineCode(outsideCodeBlocks(text));
  return [...prose.text.matchAll(FOOTNOTE)].map((match) => {
    const start = prose.offsets[match.index] as number;
    const end = prose.offsets[match.index + match[0].length - 1] as number;
    const contiguous = end - start === match[0].length - 1;
    return { n: Number(match[1]), start: contiguous ? start : null, length: match[0].length };
  });
}

/** Номера сносок, которые встречаются в тексте ответа вне кода. */
export function footnoteNumbers(text: string): Set<number> {
  return new Set(findFootnotes(text).map((footnote) => footnote.n));
}

/** Знаки частного использования Юникода: в тексте ответа они не встречаются и Markdown их не трогает. */
const MARK_OPEN = '';
const MARK_CLOSE = '';

/** Метка сноски в размеченном тексте; первая группа — номер. */
export const FOOTNOTE_MARK = new RegExp(`${MARK_OPEN}(\\d+)${MARK_CLOSE}`, 'g');

/**
 * Заменяет в тексте засчитанные сноски на источники `numbers` метками: по ним отрисовка ставит
 * кнопки-сноски ровно там, где сноску засчитал бы сервер, а не там, где код видит разбор Markdown.
 */
export function markFootnotes(text: string, numbers: ReadonlySet<number>): string {
  let marked = '';
  let position = 0;
  for (const { n, start, length } of findFootnotes(text)) {
    if (start === null || !numbers.has(n) || start < position) {
      continue;
    }
    marked += `${text.slice(position, start)}${MARK_OPEN}${n}${MARK_CLOSE}`;
    position = start + length;
  }
  return marked + text.slice(position);
}

/** Возвращает меткам вид `[n]` — там, где кнопку поставить нельзя (текст оказался внутри кода). */
export function unmarkFootnotes(text: string): string {
  return text.replace(FOOTNOTE_MARK, '[$1]');
}
