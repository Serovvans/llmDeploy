/**
 * JavaScript, HTML и CSS в редакторе «Кода» (концепция §5.13.19, контракт §7.5.16): подсветка, подсказки
 * по `api` и библиотекам клиента CoGIS, проверка синтаксиса в браузере. Часть ленивого модуля Monaco.
 */
import 'monaco-editor/languages/definitions/css/register.js';
import 'monaco-editor/languages/definitions/html/register.js';
import 'monaco-editor/languages/definitions/javascript/register.js';

import * as monaco from 'monaco-editor/editor/editor.api.js';
import { getJavaScriptWorker, javascriptDefaults } from 'monaco-editor/languages/features/typescript/register.js';
import CssWorker from 'monaco-editor/languages/features/css/css.worker.js?worker';

import type { CodeWeb } from '../../api/types';
import { extractHtml, languageOf, positionAt, syntaxMessage, type SyntaxMessage } from '../web/webRules';

// Только синтаксис и только своей проверкой: иначе каждый `api`, `ko` и поле объекта CoGIS было бы ошибкой (§5.13.19).
javascriptDefaults.setDiagnosticsOptions({ noSemanticValidation: true, noSyntaxValidation: true, noSuggestionDiagnostics: true });
// Только то, что работает и в 0.9 (концепция §5.13.6, §5.13.19): без перехода к определению, ссылок, подсмотра,
// переименования, форматирования, быстрых исправлений и подсказок внутри строки.
javascriptDefaults.setModeConfiguration({
  completionItems: true,
  hovers: true,
  documentHighlights: true,
  diagnostics: true,
  documentSymbols: false,
  definitions: false,
  references: false,
  rename: false,
  documentRangeFormattingEdits: false,
  signatureHelp: false,
  onTypeFormattingEdits: false,
  codeActions: false,
  inlayHints: false,
});

/** Воркер проверки CSS: создаётся при первой проверке файла `.css`. */
interface CssWorkerApi {
  doValidation(uri: string): Promise<{ range: { start: { line: number; character: number }; end: { line: number; character: number } }; severity?: number; message: string }[]>;
}

let cssWorker: monaco.editor.MonacoWebWorker<CssWorkerApi> | null = null;

function css(): monaco.editor.MonacoWebWorker<CssWorkerApi> {
  if (!cssWorker) {
    const worker = new CssWorker();
    // Так же, как Monaco запускает свои воркеры языков: первое сообщение будит воркер, второе — его настройки.
    worker.postMessage('ignore');
    worker.postMessage({ languageId: 'css', options: { validate: true, lint: {}, data: { useDefaultDataProvider: true } } });
    cssWorker = monaco.editor.createWebWorker<CssWorkerApi>({ worker, keepIdleModels: true });
  }
  return cssWorker;
}

/** Воркер TypeScript готов, когда Monaco встретил язык JavaScript, — до этого запрос отклоняется. */
async function jsWorker(uris: monaco.Uri[]) {
  for (let attempt = 0; ; attempt += 1) {
    try {
      const get = await getJavaScriptWorker();
      return await get(...uris);
    } catch (error) {
      if (attempt >= 20) {
        throw error;
      }
      await new Promise((resolve) => window.setTimeout(resolve, 50));
    }
  }
}

let configured: CodeWeb | null = null;

/** Описание `api` и объявления библиотек клиента CoGIS — только тех, что есть на портале (А-128). */
export async function configureWeb(web: CodeWeb): Promise<void> {
  if (configured === web) {
    return;
  }
  configured = web;
  const libs = [{ content: web.api_types, filePath: 'file:///cogis/api.d.ts' }];
  const available = web.libraries.filter((library) => library.available).map((library) => library.id);
  if (available.length > 0) {
    const { default: types } = await import('virtual:cogis-client-types');
    for (const id of available) {
      for (const file of types[id]) {
        libs.push({ content: file.content, filePath: file.path });
      }
    }
  }
  javascriptDefaults.setExtraLibs(libs);
}

/** Скрытые модели для проверки: тексты файлов и извлечённый из HTML код (смещения совпадают). */
const hidden = new Map<string, monaco.editor.ITextModel>();

function hiddenModel(name: string, text: string, language: string): monaco.editor.ITextModel {
  const uri = monaco.Uri.from({ scheme: 'portal-syntax', path: `/${encodeURIComponent(name)}` });
  let model = hidden.get(uri.toString());
  if (!model || model.isDisposed()) {
    model = monaco.editor.createModel(text, language, uri);
    hidden.set(uri.toString(), model);
  } else if (model.getValue() !== text) {
    model.setValue(text);
  }
  return model;
}

function forgetHidden(keep: Set<string>): void {
  for (const [key, model] of hidden) {
    if (!keep.has(key)) {
      model.dispose();
      hidden.delete(key);
    }
  }
}

interface TsDiagnostic {
  start?: number;
  length?: number;
  messageText: string | { messageText: string };
}

function tsText(diagnostic: TsDiagnostic): string {
  return syntaxMessage(typeof diagnostic.messageText === 'string' ? diagnostic.messageText : diagnostic.messageText.messageText);
}

/** Ошибка по смещениям исходного файла. */
function message(path: string, text: string, start: number, end: number, value: string): SyntaxMessage {
  const from = positionAt(text, start);
  const to = positionAt(text, Math.max(end, start + 1));
  return { path, line: from.line, column: from.column, endLine: to.line, endColumn: to.column, message: value };
}

/**
 * Проверка синтаксиса всех файлов проекта (§5.13.19): `.js` целиком, `.css` целиком, в `.html` — код в
 * `<script>` и значения `data-bind`. Только ошибки, без «неизвестных имён». Порядок — по путям.
 */
export async function checkSyntax(files: readonly { path: string; content: string }[]): Promise<SyntaxMessage[]> {
  const keep = new Set<string>();
  const result: SyntaxMessage[] = [];
  const sorted = [...files].sort((a, b) => a.path.localeCompare(b.path));
  for (const file of sorted) {
    const language = languageOf(file.path);
    if (language === 'javascript') {
      const model = hiddenModel(`${file.path}.js`, file.content, 'javascript');
      keep.add(model.uri.toString());
      const worker = await jsWorker([model.uri]);
      const diagnostics = (await worker.getSyntacticDiagnostics(model.uri.toString())) as TsDiagnostic[];
      for (const item of diagnostics) {
        const start = item.start ?? 0;
        result.push(message(file.path, file.content, start, start + (item.length ?? 0), tsText(item)));
      }
    } else if (language === 'html') {
      const code = extractHtml(file.content);
      const script = hiddenModel(`${file.path}.script.js`, code.script, 'javascript');
      const bindings = hiddenModel(`${file.path}.bind.js`, code.bindings.text, 'javascript');
      keep.add(script.uri.toString());
      keep.add(bindings.uri.toString());
      const worker = await jsWorker([script.uri, bindings.uri]);
      for (const item of (await worker.getSyntacticDiagnostics(script.uri.toString())) as TsDiagnostic[]) {
        const start = item.start ?? 0;
        result.push(message(file.path, file.content, start, start + (item.length ?? 0), tsText(item)));
      }
      const map = code.bindings.map;
      const source = (offset: number) => map[Math.min(Math.max(offset, 0), map.length - 1)] ?? 0;
      for (const item of (await worker.getSyntacticDiagnostics(bindings.uri.toString())) as TsDiagnostic[]) {
        const start = item.start ?? 0;
        result.push(message(file.path, file.content, source(start), source(start + (item.length ?? 0)), tsText(item)));
      }
    } else if (language === 'css') {
      const model = hiddenModel(`${file.path}.css`, file.content, 'css');
      keep.add(model.uri.toString());
      const worker = await css().withSyncedResources([model.uri]);
      for (const item of await worker.doValidation(model.uri.toString())) {
        if (item.severity !== 1) {
          continue;
        }
        result.push({
          path: file.path,
          line: item.range.start.line + 1,
          column: item.range.start.character + 1,
          endLine: item.range.end.line + 1,
          endColumn: item.range.end.character + 1,
          message: syntaxMessage(item.message),
        });
      }
    }
  }
  forgetHidden(keep);
  return result;
}

/** Число ошибок синтаксиса — для окна «Скачать для CoGIS», открытого из списка проектов (§5.13.22). */
export async function countSyntaxErrors(files: readonly { path: string; content: string }[]): Promise<number> {
  return (await checkSyntax(files)).length;
}

/* ---------- Подсказки внутри `<script>` HTML-файла: тем же воркером, по скрытой модели ---------- */

type CompletionKind = monaco.languages.CompletionItemKind;

function completionKind(kind: string): CompletionKind {
  const k = monaco.languages.CompletionItemKind;
  switch (kind) {
    case 'method':
    case 'function':
    case 'local function':
      return k.Function;
    case 'property':
    case 'getter':
    case 'setter':
      return k.Property;
    case 'class':
      return k.Class;
    case 'interface':
      return k.Interface;
    case 'module':
      return k.Module;
    case 'keyword':
      return k.Keyword;
    default:
      return k.Variable;
  }
}

interface DisplayPart {
  text: string;
}

const partsText = (parts: DisplayPart[] | undefined) => (parts ?? []).map((part) => part.text).join('');

/** Скрытая модель скриптов открытого HTML и смещение курсора — если курсор внутри `<script>`. */
function scriptAt(model: monaco.editor.ITextModel, position: monaco.Position) {
  const text = model.getValue();
  const offset = model.getOffsetAt(position);
  const code = extractHtml(text);
  if (!code.scripts.some(([start, end]) => offset >= start && offset <= end)) {
    return null;
  }
  const name = `${model.uri.path}.edit.js`;
  return { script: hiddenModel(name, code.script, 'javascript'), offset };
}

type ScriptSuggestion = monaco.languages.CompletionItem & { uri: monaco.Uri; offset: number };

let htmlRegistered = false;

/** Подсказки и описание при наведении в `<script>` HTML — как в файле `.js` (§5.13.19). */
export function registerHtmlScriptHints(): void {
  if (htmlRegistered) {
    return;
  }
  htmlRegistered = true;
  monaco.languages.registerCompletionItemProvider('html', {
    triggerCharacters: ['.'],
    async provideCompletionItems(model, position) {
      const found = scriptAt(model, position);
      if (!found) {
        return { suggestions: [] };
      }
      const worker = await jsWorker([found.script.uri]);
      const info = (await worker.getCompletionsAtPosition(found.script.uri.toString(), found.offset)) as
        | { entries: { name: string; kind: string; sortText: string }[] }
        | undefined;
      const word = model.getWordUntilPosition(position);
      const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
      const suggestions = (info?.entries ?? []).map((entry) => {
        // `uri` and `offset` travel with the item to `resolveCompletionItem`: signature and description are asked lazily.
        const suggestion: ScriptSuggestion = {
          label: entry.name,
          kind: completionKind(entry.kind),
          sortText: entry.sortText,
          insertText: entry.name,
          range,
          uri: found.script.uri,
          offset: found.offset,
        };
        return suggestion;
      });
      return { suggestions };
    },
    async resolveCompletionItem(item) {
      const extra = item as Partial<ScriptSuggestion>;
      if (!extra.uri || extra.offset === undefined) {
        return item;
      }
      const worker = await jsWorker([extra.uri]);
      const details = (await worker.getCompletionEntryDetails(extra.uri.toString(), extra.offset, String(item.label))) as
        | { displayParts?: DisplayPart[]; documentation?: DisplayPart[] }
        | undefined;
      return { ...item, detail: partsText(details?.displayParts), documentation: partsText(details?.documentation) };
    },
  });
  monaco.languages.registerHoverProvider('html', {
    async provideHover(model, position) {
      const found = scriptAt(model, position);
      if (!found) {
        return null;
      }
      const worker = await jsWorker([found.script.uri]);
      const info = (await worker.getQuickInfoAtPosition(found.script.uri.toString(), found.offset)) as
        | { displayParts?: DisplayPart[]; documentation?: DisplayPart[]; textSpan: { start: number; length: number } }
        | undefined;
      if (!info) {
        return null;
      }
      const start = model.getPositionAt(info.textSpan.start);
      const end = model.getPositionAt(info.textSpan.start + info.textSpan.length);
      const documentation = partsText(info.documentation);
      return {
        range: new monaco.Range(start.lineNumber, start.column, end.lineNumber, end.column),
        contents: [{ value: `\`\`\`javascript\n${partsText(info.displayParts)}\n\`\`\`` }, ...(documentation ? [{ value: documentation }] : [])],
      };
    },
  });
}
