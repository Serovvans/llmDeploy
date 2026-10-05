/** Подсветка кода: lowlight (обёртка над highlight.js) отдаёт дерево, а не разметку — вставки HTML нет. */
import bash from 'highlight.js/lib/languages/bash';
import csharp from 'highlight.js/lib/languages/csharp';
import json from 'highlight.js/lib/languages/json';
import python from 'highlight.js/lib/languages/python';
import sql from 'highlight.js/lib/languages/sql';
import xml from 'highlight.js/lib/languages/xml';
import { createLowlight } from 'lowlight';

import { texts } from '../../texts';

const lowlight = createLowlight({ bash, csharp, json, python, sql, xml });

/** Как язык пишут в ответе → язык подсветки и название в шапке блока (концепция §4.2). */
const LANGUAGES: Record<string, { grammar: string; title: string }> = {
  sql: { grammar: 'sql', title: 'SQL' },
  csharp: { grammar: 'csharp', title: 'C#' },
  cs: { grammar: 'csharp', title: 'C#' },
  'c#': { grammar: 'csharp', title: 'C#' },
  json: { grammar: 'json', title: 'JSON' },
  xml: { grammar: 'xml', title: 'XML' },
  html: { grammar: 'xml', title: 'HTML' },
  python: { grammar: 'python', title: 'Python' },
  py: { grammar: 'python', title: 'Python' },
  bash: { grammar: 'bash', title: 'Bash' },
  sh: { grammar: 'bash', title: 'Bash' },
  shell: { grammar: 'bash', title: 'Bash' },
};

export type HighlightNode = ReturnType<typeof lowlight.highlight>['children'][number];

export interface Highlighted {
  title: string;
  /** `null` — язык не из списка: код выводится без подсветки. */
  nodes: HighlightNode[] | null;
}

export function highlight(code: string, language: string | null): Highlighted {
  if (!language) {
    return { title: texts.chat.code.text, nodes: null };
  }
  const known = LANGUAGES[language.toLowerCase()];
  if (!known) {
    return { title: language, nodes: null };
  }
  return { title: known.title, nodes: lowlight.highlight(known.grammar, code).children };
}
