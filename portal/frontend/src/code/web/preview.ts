/**
 * Обмен с изолированной страницей предпросмотра (контракт §7.5.17, концепция §5.13.21): разбор сообщений
 * фрейма, придуманный слой, тексты журнала. Сообщения фрейма — недоверенные данные: принимаются только
 * известные типы с полями своих типов, строки обрезаются и показываются только как текст.
 */
import type { CatalogClient, CatalogPlacement, CodeKind, GeoJsonFeatureCollection } from '../../api/types';
import { texts } from '../../texts';

const p = texts.workshop.web.pane;

/** Предел длины любой строки из фрейма (§7.5.17). */
export const MAX_TEXT = 2000;
const MAX_ARGS = 20;

export type FrameMessage =
  | { type: 'ready' }
  | { type: 'log'; fn: string; args: string[]; known: boolean; count: number | null }
  | { type: 'error'; file: string | null; line: number | null; column: number | null; message: string }
  | { type: 'blocked'; url: string; directive: string };

const isObject = (value: unknown): value is Record<string, unknown> => typeof value === 'object' && value !== null && !Array.isArray(value);
const cut = (value: string) => value.slice(0, MAX_TEXT);
const isCount = (value: unknown): value is number | null => value === null || (typeof value === 'number' && Number.isInteger(value) && value >= 0);
const isLine = (value: unknown): value is number | null => value === null || (typeof value === 'number' && Number.isInteger(value) && value > 0);

/** Сообщение фрейма или `null`, если тип неизвестен или поля не своих типов — такое отбрасывается. */
export function parseFrameMessage(data: unknown): FrameMessage | null {
  if (!isObject(data)) {
    return null;
  }
  switch (data.type) {
    case 'ready':
      return { type: 'ready' };
    case 'log': {
      const { fn, args, known, count } = data;
      if (typeof fn !== 'string' || !Array.isArray(args) || !args.every((arg) => typeof arg === 'string') || typeof known !== 'boolean' || !isCount(count)) {
        return null;
      }
      return { type: 'log', fn: cut(fn), args: (args as string[]).slice(0, MAX_ARGS).map(cut), known, count };
    }
    case 'error': {
      const { file, line, column, message } = data;
      if ((file !== null && typeof file !== 'string') || !isLine(line) || !isLine(column) || typeof message !== 'string') {
        return null;
      }
      return { type: 'error', file: file === null ? null : cut(file), line, column, message: cut(message) };
    }
    case 'blocked': {
      const { url, directive } = data;
      if (typeof url !== 'string' || typeof directive !== 'string') {
        return null;
      }
      return { type: 'blocked', url: cut(url), directive: cut(directive) };
    }
    default:
      return null;
  }
}

export interface LayerSource {
  /** `null` — придуманный слой. */
  table: string | null;
  rows: number;
}

const NOT_RUN = new Set(['geocodeAddress', 'findRoute', 'constructServiceArea', 'generateReport']);
const SYMBOLS = new Set(['createSimplePointSymbol', 'createPicturePointSymbol', 'createSimpleLineSymbol', 'createSimpleFillSymbol']);

/** «→ что сделано» по функции (таблица §5.13.21). */
export function logResult(entry: { fn: string; args: string[]; known: boolean; count: number | null }, layer: LayerSource): string {
  const r = p.results;
  if (!entry.known) {
    return r.unknown;
  }
  const { fn } = entry;
  if (fn === 'getLayer') {
    return layer.table ? r.layerSample(layer.table, entry.count ?? layer.rows) : r.layer;
  }
  if (fn === 'searchInLayer') {
    return r.search(entry.count ?? 0);
  }
  if (fn === 'createPoint' || fn === 'createExtent' || fn === 'getCentroid' || fn === 'createColor') {
    return r[fn];
  }
  if (fn === 'getSpatialReference') {
    return r.getSpatialReference(entry.args[0] ?? '');
  }
  if (SYMBOLS.has(fn)) {
    return r.symbol;
  }
  if (fn === 'project') {
    return r.project;
  }
  if (fn === 'showAlert') {
    return r.showAlert;
  }
  if (NOT_RUN.has(fn)) {
    return r.notRun;
  }
  // Карта, фильтры, сведения и таблица атрибутов (§5.13.21).
  return r.recorded;
}

export interface LogEntry {
  fn: string;
  args: string[];
  known: boolean;
  count: number | null;
}

export interface PreviewProblem {
  kind: 'error' | 'network' | 'image';
  file: string | null;
  line: number | null;
  column: number | null;
  /** Текст ошибки браузера как есть или адрес запрета. */
  text: string;
  /** Директива CSP у запрета; у ошибки — `null`. */
  directive: string | null;
}

export interface PreviewLog {
  entries: LogEntry[];
  /** Записей было больше предела — последняя строка журнала об этом (§5.13.21). */
  overflow: boolean;
  problems: PreviewProblem[];
}

export const EMPTY_LOG: PreviewLog = { entries: [], overflow: false, problems: [] };

/** Следующее состояние журнала: сверх пределов записи и ошибки отбрасываются (§7.5.17). */
export function appendMessage(log: PreviewLog, message: FrameMessage, limits: { log_max_entries: number; errors_max: number }): PreviewLog {
  if (message.type === 'log') {
    if (log.entries.length >= limits.log_max_entries) {
      return log.overflow ? log : { ...log, overflow: true };
    }
    return { ...log, entries: [...log.entries, { fn: message.fn, args: message.args, known: message.known, count: message.count }] };
  }
  if (message.type === 'ready' || log.problems.length >= limits.errors_max) {
    return log;
  }
  const problem: PreviewProblem =
    message.type === 'error'
      ? { kind: 'error', file: message.file, line: message.line, column: message.column, text: message.message, directive: null }
      : {
          kind: message.directive.startsWith('img-src') ? 'image' : 'network',
          file: null,
          line: null,
          column: null,
          text: message.url,
          directive: message.directive,
        };
  return { ...log, problems: [...log.problems, problem] };
}

/** Тип ошибки браузера из её текста («Uncaught TypeError: …» → `TypeError`); не распознан — `Error`. */
export function errorName(text: string): string {
  return /^(?:Uncaught (?:\(in promise\) )?)?([A-Za-z_$][\w$]*(?:Error|Exception))\b/.exec(text)?.[1] ?? 'Error';
}

/**
 * Строки вопроса «Исправить с помощником» по ошибкам предпросмотра (§5.13.20). Слой из образца базы
 * заказчика — только файл, строка, тип ошибки и директива запрета: текст ошибки и адрес могут нести
 * значения строк базы, а вопрос уходит модели и в историю (ПД §5.8, §8.6).
 */
export function previewFixItems(problems: readonly PreviewProblem[], fromSample: boolean): { path: string | null; line: number | null; message: string }[] {
  const pane = texts.workshop.web.pane;
  return problems
    .filter((problem) => problem.kind !== 'image')
    .map((problem) => {
      if (problem.kind === 'network') {
        return { path: problem.file, line: problem.line, message: fromSample ? pane.networkDirective(problem.directive ?? '') : pane.network(problem.text) };
      }
      return { path: problem.file, line: problem.line, message: fromSample ? errorName(problem.text) : problem.text };
    });
}

/** Ошибки кода — то, что считается на вкладке и на станции; запрет картинки ошибкой не считается (§5.13.21). */
export function errorCount(log: PreviewLog): number {
  return log.problems.filter((problem) => problem.kind !== 'image').length;
}

/** Адрес в строке запрета — до 80 знаков (§5.13.21). */
export function shortUrl(url: string): string {
  return url.length > 80 ? `${url.slice(0, 79)}…` : url;
}

/** Придуманный слой (контракт §7.5.17): пять точек в WGS 84 рядом с примером документации. */
export const INVENTED_FEATURES: GeoJsonFeatureCollection = {
  type: 'FeatureCollection',
  features: [
    [1, 'Участок 1', 'Аренда', 82.9246, 55.0304],
    [2, 'Участок 2', 'Собственность', 82.9346, 55.0354],
    [3, 'Участок 3', 'Аренда', 82.9446, 55.0254],
    [4, 'Участок 4', 'Свободен', 82.9146, 55.0404],
    [5, 'Участок 5', 'Аренда', 82.9546, 55.0204],
  ].map(([id, name, status, x, y]) => ({
    type: 'Feature' as const,
    geometry: { type: 'Point', coordinates: [x, y] },
    properties: { id: String(id), name: String(name), status: String(status) },
  })),
};

export const INVENTED_LAYER_NAME = 'Слой предпросмотра';

export interface InitMessage {
  type: 'init';
  kind: CodeKind;
  files: { path: string; content: string; catalog: CatalogPlacement | null }[];
  client: CatalogClient | null;
  layer: { source: 'invented' | 'sample'; name: string; features: GeoJsonFeatureCollection };
  libraries: { jquery: string | null; knockout: string | null; leaflet: string | null; leaflet_css: string | null };
  api_functions: string[];
  limits: { log_max_entries: number; errors_max: number };
}

/** Файлы, которые не вставлены для выбранного типа клиента Каталога (§5.13.21). */
export function notInserted(files: readonly { path: string; catalog?: CatalogPlacement | null }[], client: CatalogClient): string[] {
  return files.filter((file) => file.catalog && !file.catalog.clients.includes(client)).map((file) => file.path);
}

/** Ширина страницы Каталога по типу клиента (§7.5.17). */
export const CLIENT_WIDTH: Record<CatalogClient, number> = { desktop: 1280, tablet: 768, smartphone: 375 };
