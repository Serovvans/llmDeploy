/**
 * Виды проектов «Кода» (концепция §5.13.17, контракт §7.5.16): названия и места в CoGIS — из словаря,
 * расширения и признак «один файл» — из `GET /api/config` (`code.kinds`).
 */
import type { CodeConfig, CodeKind, CodeProject, CodeProjectItem } from '../api/types';
import { texts } from '../texts';

export type WebKind = Exclude<CodeKind, 'gptool'>;

/** Порядок строк на начале раздела (§5.13.17). */
export const KINDS: readonly CodeKind[] = ['gptool', 'js_plugin', 'html_widget', 'catalog', 'card_hooks'];

/** Вид проекта; у ответов до 1.0 поля нет — это GPTool. */
export function kindOf(project: Pick<CodeProject | CodeProjectItem, 'kind'>): CodeKind {
  return project.kind ?? 'gptool';
}

export function isWebKind(kind: CodeKind): kind is WebKind {
  return kind !== 'gptool';
}

export function kindTexts(kind: CodeKind) {
  return texts.workshop.web.kinds[kind];
}

/** Расширения и «один файл» вида; без `code.kinds` (ответ до 1.0) — расширения GPTool. */
export function kindRules(config: CodeConfig, kind: CodeKind): { extensions: string[]; singleFile: boolean } {
  const known = config.kinds?.find((item) => item.id === kind);
  if (known) {
    return { extensions: known.extensions, singleFile: known.single_file };
  }
  return { extensions: kind === 'gptool' ? config.file_extensions : [], singleFile: false };
}

/** «.css, .html и .js» — расширения вида для текстов ошибок (§5.13.17). */
export function extensionList(extensions: readonly string[]): string {
  if (extensions.length <= 1) {
    return extensions[0] ?? '';
  }
  return `${extensions.slice(0, -1).join(', ')} и ${extensions.at(-1) ?? ''}`;
}

/** Ошибка неверного расширения нового или переименованного файла веб-вида (§5.13.17). */
export function wrongExtensionText(config: CodeConfig, kind: CodeKind): string {
  const { extensions, singleFile } = kindRules(config, kind);
  const web = texts.workshop.web.files;
  return singleFile ? web.onlyOne(extensions[0] ?? '') : web.only(extensionList(extensions));
}
