/**
 * Подсказки редактора по справочнику CoGIS и частым типам .NET (концепция §5.13.6, контракт §7.5.14):
 * без языкового сервера — по тексту строки до курсора. После «Тип.» — члены этого типа или типы
 * пространства имён, иначе — имена типов.
 */
import type { SdkMember, SdkType } from '../api/types';

export type SuggestionKind = SdkType['kind'] | SdkMember['kind'] | 'namespace';

export interface Suggestion {
  label: string;
  kind: SuggestionKind;
  /** Объявление — серым в списке подсказок. */
  detail: string;
  /** Перегрузки и полное имя — в описании пункта. */
  documentation: string;
}

/** Имя типа без параметров типа: `List<T>` и ``List`1`` → `List`. */
export function baseName(name: string): string {
  return name.replace(/[<`].*$/, '');
}

const QUALIFIED = /([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\.[A-Za-z0-9_]*$/;

function memberSuggestions(types: SdkType[]): Suggestion[] {
  const byName = new Map<string, Suggestion>();
  for (const type of types) {
    for (const member of type.members) {
      if (member.kind === 'constructor') {
        continue;
      }
      const known = byName.get(member.name);
      if (known) {
        known.documentation = `${known.documentation}\n${member.signature}`;
      } else {
        byName.set(member.name, { label: member.name, kind: member.kind, detail: member.signature, documentation: member.signature });
      }
    }
  }
  return [...byName.values()];
}

function typeSuggestion(type: SdkType): Suggestion {
  return {
    label: baseName(type.name),
    kind: type.kind,
    detail: type.signature,
    documentation: `${type.namespace}.${type.name}`,
  };
}

export function suggestions(types: readonly SdkType[], lineBefore: string): Suggestion[] {
  const qualified = QUALIFIED.exec(lineBefore)?.[1];
  if (qualified) {
    const last = qualified.split('.').at(-1) ?? qualified;
    const owners = types.filter((type) => baseName(type.name) === last || `${type.namespace}.${baseName(type.name)}` === qualified);
    if (owners.length > 0) {
      return memberSuggestions(owners);
    }
    // «eLiteGIS.Geoprocessing.» — типы этого пространства имён и вложенные пространства.
    const inside = types.filter((type) => type.namespace === qualified || type.namespace.endsWith(`.${qualified}`));
    const nested = new Set(
      types
        .map((type) => type.namespace)
        .filter((namespace) => namespace.startsWith(`${qualified}.`))
        .map((namespace) => namespace.slice(qualified.length + 1).split('.')[0] ?? ''),
    );
    return [
      ...[...nested].map((name) => ({ label: name, kind: 'namespace' as const, detail: `namespace ${qualified}.${name}`, documentation: '' })),
      ...inside.map(typeSuggestion),
    ];
  }
  const seen = new Set<string>();
  return types.map(typeSuggestion).filter((item) => {
    const key = `${item.label}|${item.documentation}`;
    if (seen.has(key)) {
      return false;
    }
    seen.add(key);
    return true;
  });
}
