/**
 * Сборка Monaco для раздела «Код» (концепция §5.13.6, контракт §1.1): только нужные возможности,
 * русский перевод той же версии, воркер — файлом своего origin. Модуль грузится лениво.
 */
// Перевод — первым: подписи Monaco читаются при загрузке его модулей.
import 'monaco-editor/nls/lang/ru.js';

import 'monaco-editor/features/codicon/register.js';
import 'monaco-editor/editor/browser/coreCommands.js';
import 'monaco-editor/editor/browser/widget/codeEditor/codeEditorWidget.js';
import 'monaco-editor/editor/browser/widget/diffEditor/diffEditor.contribution.js';
import 'monaco-editor/editor/common/standaloneStrings.js';
import 'monaco-editor/editor/contrib/bracketMatching/browser/bracketMatching.js';
import 'monaco-editor/editor/contrib/clipboard/browser/clipboard.js';
import 'monaco-editor/editor/contrib/comment/browser/comment.js';
import 'monaco-editor/editor/contrib/contextmenu/browser/contextmenu.js';
import 'monaco-editor/editor/contrib/cursorUndo/browser/cursorUndo.js';
import 'monaco-editor/editor/contrib/folding/browser/folding.js';
import 'monaco-editor/editor/contrib/gotoError/browser/gotoError.js';
import 'monaco-editor/editor/contrib/gotoError/browser/markerSelectionStatus.js';
import 'monaco-editor/editor/contrib/hover/browser/hoverContribution.js';
import 'monaco-editor/editor/contrib/indentation/browser/indentation.js';
import 'monaco-editor/editor/contrib/linesOperations/browser/linesOperations.js';
import 'monaco-editor/editor/contrib/multicursor/browser/multicursor.js';
import 'monaco-editor/editor/contrib/readOnlyMessage/browser/contribution.js';
import 'monaco-editor/editor/contrib/suggest/browser/suggestController.js';
import 'monaco-editor/editor/contrib/toggleTabFocusMode/browser/toggleTabFocusMode.js';
import 'monaco-editor/editor/contrib/wordHighlighter/browser/wordHighlighter.js';
import 'monaco-editor/editor/contrib/wordOperations/browser/wordOperations.js';
import 'monaco-editor/editor/standalone/browser/quickAccess/standaloneGotoLineQuickAccess.js';
import 'monaco-editor/features/find/register.js';
import 'monaco-editor/languages/definitions/csharp/register.js';

import * as monaco from 'monaco-editor/editor/editor.api.js';
import EditorWorker from 'monaco-editor/editor/editor.worker.js?worker';
import CssWorker from 'monaco-editor/languages/features/css/css.worker.js?worker';

import type { CodeSdk } from '../../api/types';
import { CODE_COLORS } from '../../theme/codeColors';
import type { ResolvedTheme } from '../../theme/preference';
import { suggestions, type SuggestionKind } from '../sdkCompletion';
import './monaco.css';
import TsWorker from './ts.worker.ts?worker';

export { checkSyntax, configureWeb, countSyntaxErrors, registerHtmlScriptHints } from './web';

// Воркеры — отдельные файлы своего origin: `default-src 'self'` их разрешает, `blob:` не нужен (А-113).
// Воркер TypeScript и воркер CSS грузятся только при открытии файла JavaScript, HTML или CSS (1.0).
self.MonacoEnvironment = {
  getWorker: (_moduleId: string, label: string) => {
    if (label === 'javascript' || label === 'typescript') {
      return new TsWorker();
    }
    if (label === 'css') {
      return new CssWorker();
    }
    return new EditorWorker();
  },
};

export type Monaco = typeof monaco;
export { monaco };

/** Цвет переменной портала числом: Monaco не понимает `var()` и `color-mix` (концепция §7.3). */
function resolveColors(names: readonly string[]): Record<string, [number, number, number, number]> {
  const probe = document.createElement('span');
  probe.style.display = 'none';
  document.body.append(probe);
  const result: Record<string, [number, number, number, number]> = {};
  for (const name of names) {
    probe.style.color = `var(${name})`;
    result[name] = parseColor(getComputedStyle(probe).color);
  }
  probe.remove();
  return result;
}

/** `rgb()`, `rgba()` и `color(srgb …)` — так браузер отдаёт вычисленный цвет, в том числе смесь. */
export function parseColor(value: string): [number, number, number, number] {
  const srgb = /color\(srgb\s+([\d.e-]+)\s+([\d.e-]+)\s+([\d.e-]+)(?:\s*\/\s*([\d.e-]+))?\)/.exec(value);
  if (srgb) {
    const [r, g, b] = [srgb[1], srgb[2], srgb[3]].map((part) => Math.round(Number(part) * 255));
    return [r ?? 0, g ?? 0, b ?? 0, srgb[4] === undefined ? 1 : Number(srgb[4])];
  }
  const rgb = /rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+))?\s*\)/.exec(value);
  if (rgb) {
    return [Number(rgb[1]), Number(rgb[2]), Number(rgb[3]), rgb[4] === undefined ? 1 : Number(rgb[4])];
  }
  return [0, 0, 0, 1];
}

/** Полупрозрачный цвет на фоне — непрозрачным: так его рисует и портал. */
function flatten([r, g, b, a]: [number, number, number, number], [br, bg, bb]: [number, number, number, number]): string {
  const mix = (top: number, bottom: number) => Math.round(top * a + bottom * (1 - a));
  return `#${[mix(r, br), mix(g, bg), mix(b, bb)].map((part) => part.toString(16).padStart(2, '0')).join('')}`;
}

const VARIABLES = [
  '--p-bg',
  '--p-text',
  '--p-text-muted',
  '--p-surface-2',
  '--p-surface-3',
  '--p-line',
  '--p-focus',
  '--p-error-text',
  '--p-error-bg',
  '--p-warning-bg',
  '--p-warning-icon',
  '--p-mark-bg',
  '--p-added-bg',
] as const;

/** Темы Monaco из текущей темы портала — таблица соответствия концепции §7.3. */
export function applyTheme(theme: ResolvedTheme): void {
  const raw = resolveColors(VARIABLES);
  const base = raw['--p-bg'] ?? [255, 255, 255, 1];
  const c = (name: (typeof VARIABLES)[number]) => flatten(raw[name] ?? base, base);
  const code = CODE_COLORS[theme];
  const plain = (hex: string) => hex.replace('#', '');
  const name = `portal-${theme}`;
  monaco.editor.defineTheme(name, {
    base: theme === 'dark' ? 'vs-dark' : 'vs',
    inherit: true,
    rules: [
      { token: '', foreground: plain(c('--p-text')) },
      { token: 'keyword', foreground: plain(code.keyword) },
      { token: 'string', foreground: plain(code.string) },
      { token: 'string.escape', foreground: plain(code.string) },
      { token: 'number', foreground: plain(code.number) },
      { token: 'type', foreground: plain(code.type) },
      { token: 'type.identifier', foreground: plain(code.type) },
      { token: 'comment', foreground: plain(code.comment) },
      { token: 'identifier', foreground: plain(c('--p-text')) },
      { token: 'delimiter', foreground: plain(c('--p-text')) },
      { token: 'namespace', foreground: plain(c('--p-text')) },
      { token: 'annotation', foreground: plain(c('--p-text')) },
      // JavaScript, HTML и CSS (1.0): у HTML теги — ключевые слова, атрибуты — типы, значения — строки;
      // у CSS селекторы — типы, свойства — ключевые слова, значения — строки и числа (§7.3).
      { token: 'regexp', foreground: plain(code.string) },
      { token: 'metatag', foreground: plain(code.keyword) },
      { token: 'tag', foreground: plain(code.keyword) },
      { token: 'attribute.name', foreground: plain(code.type) },
      { token: 'attribute.value', foreground: plain(code.string) },
      { token: 'tag.css', foreground: plain(code.type) },
      { token: 'attribute.name.css', foreground: plain(code.keyword) },
      { token: 'attribute.value.css', foreground: plain(code.string) },
      { token: 'attribute.value.number.css', foreground: plain(code.number) },
      { token: 'attribute.value.unit.css', foreground: plain(code.number) },
      { token: 'attribute.value.hex.css', foreground: plain(code.number) },
    ],
    colors: {
      'editor.background': c('--p-bg'),
      'editorGutter.background': c('--p-bg'),
      'editor.foreground': c('--p-text'),
      'editorCursor.foreground': c('--p-text'),
      'editorLineNumber.activeForeground': c('--p-text'),
      'editorLineNumber.foreground': c('--p-text-muted'),
      'editor.foldPlaceholderForeground': c('--p-text-muted'),
      'editorGutter.foldingControlForeground': c('--p-text-muted'),
      'editor.lineHighlightBackground': c('--p-surface-2'),
      'editor.lineHighlightBorder': c('--p-surface-2'),
      'editor.selectionBackground': c('--p-mark-bg'),
      'editor.inactiveSelectionBackground': c('--p-mark-bg'),
      'editor.findMatchBackground': c('--p-warning-bg'),
      'editor.findMatchHighlightBackground': c('--p-warning-bg'),
      'editorIndentGuide.background1': c('--p-line'),
      'editorIndentGuide.activeBackground1': c('--p-line'),
      'editorWidget.background': c('--p-bg'),
      'editorWidget.foreground': c('--p-text'),
      'editorWidget.border': c('--p-line'),
      'editorSuggestWidget.background': c('--p-bg'),
      'editorSuggestWidget.border': c('--p-line'),
      'editorSuggestWidget.foreground': c('--p-text'),
      'editorSuggestWidget.selectedBackground': c('--p-surface-3'),
      'editorSuggestWidget.selectedForeground': c('--p-text'),
      'editorHoverWidget.background': c('--p-bg'),
      'editorHoverWidget.border': c('--p-line'),
      'editorHoverWidget.foreground': c('--p-text'),
      'menu.background': c('--p-bg'),
      'menu.foreground': c('--p-text'),
      'menu.border': c('--p-line'),
      'menu.selectionBackground': c('--p-surface-3'),
      'menu.selectionForeground': c('--p-text'),
      'menu.separatorBackground': c('--p-line'),
      'quickInput.background': c('--p-bg'),
      'quickInput.foreground': c('--p-text'),
      'quickInputList.focusBackground': c('--p-surface-3'),
      'quickInputList.focusForeground': c('--p-text'),
      'list.activeSelectionBackground': c('--p-surface-3'),
      'list.activeSelectionForeground': c('--p-text'),
      'list.focusBackground': c('--p-surface-3'),
      'list.focusForeground': c('--p-text'),
      'list.hoverBackground': c('--p-surface-2'),
      'list.hoverForeground': c('--p-text'),
      'input.background': c('--p-bg'),
      'input.foreground': c('--p-text'),
      'input.border': c('--p-line'),
      focusBorder: c('--p-focus'),
      'editorError.foreground': c('--p-error-text'),
      'editorWarning.foreground': c('--p-warning-icon'),
      'editorOverviewRuler.errorForeground': c('--p-error-text'),
      'editorOverviewRuler.warningForeground': c('--p-warning-icon'),
      'scrollbarSlider.background': c('--p-surface-3'),
      'scrollbarSlider.hoverBackground': c('--p-surface-3'),
      'scrollbarSlider.activeBackground': c('--p-surface-3'),
      'diffEditor.insertedLineBackground': c('--p-added-bg'),
      'diffEditor.insertedTextBackground': c('--p-added-bg'),
      'diffEditor.removedLineBackground': c('--p-error-bg'),
      'diffEditor.removedTextBackground': c('--p-error-bg'),
      'diffEditorGutter.insertedLineBackground': c('--p-added-bg'),
      'diffEditorGutter.removedLineBackground': c('--p-error-bg'),
      'diffEditor.diagonalFill': c('--p-line'),
    },
  });
  monaco.editor.setTheme(name);
}

const KINDS: Record<SuggestionKind, monaco.languages.CompletionItemKind> = {
  class: monaco.languages.CompletionItemKind.Class,
  struct: monaco.languages.CompletionItemKind.Struct,
  interface: monaco.languages.CompletionItemKind.Interface,
  enum: monaco.languages.CompletionItemKind.Enum,
  delegate: monaco.languages.CompletionItemKind.Function,
  constructor: monaco.languages.CompletionItemKind.Constructor,
  method: monaco.languages.CompletionItemKind.Method,
  property: monaco.languages.CompletionItemKind.Property,
  field: monaco.languages.CompletionItemKind.Field,
  event: monaco.languages.CompletionItemKind.Event,
  enum_value: monaco.languages.CompletionItemKind.EnumMember,
  namespace: monaco.languages.CompletionItemKind.Module,
};

let sdkTypes: CodeSdk['types'] = [];
let registered = false;

/** Справочник для подсказок: один на раздел, `GET /api/code/sdk` при его открытии. */
export function setSdkTypes(types: CodeSdk['types']): void {
  sdkTypes = types;
  if (registered) {
    return;
  }
  registered = true;
  monaco.languages.registerCompletionItemProvider('csharp', {
    triggerCharacters: ['.'],
    provideCompletionItems(model, position) {
      const word = model.getWordUntilPosition(position);
      const range = new monaco.Range(position.lineNumber, word.startColumn, position.lineNumber, word.endColumn);
      const before = model.getLineContent(position.lineNumber).slice(0, position.column - 1);
      return {
        suggestions: suggestions(sdkTypes, before).map((item) => ({
          label: item.label,
          kind: KINDS[item.kind],
          detail: item.detail,
          documentation: item.documentation,
          insertText: item.label,
          range,
        })),
      };
    },
  });
}

/** JetBrains Mono мог загрузиться позже создания редактора — ширина знаков пересчитывается (§7.3). */
export function remeasureWhenFontsReady(): void {
  void document.fonts?.load('14px "JetBrains Mono"').then(() => monaco.editor.remeasureFonts());
}
