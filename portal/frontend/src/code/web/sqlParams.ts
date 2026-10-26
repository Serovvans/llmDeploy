/**
 * «Проверить запрос» (концепция §5.13.23, контракт §7.5.18): параметры запроса тем же правилом, что сервер,
 * и кавычки вокруг выделения.
 */

/**
 * Параметры в порядке появления, без повторов: `@имя`, если перед `@` не буква, цифра, `_`, `@` или `<`;
 * `$n`, если перед `$` не буква, цифра, `_` или `$`. Строки, комментарии, имена в кавычках и `$…$` пропускаются.
 */
export function sqlParameters(sql: string): string[] {
  const found: string[] = [];
  const add = (name: string) => {
    if (!found.includes(name)) {
      found.push(name);
    }
  };
  let index = 0;
  while (index < sql.length) {
    const char = sql[index] ?? '';
    const next = sql[index + 1] ?? '';
    if (char === "'" || char === '"') {
      // Строка или имя в кавычках; удвоенная кавычка — часть текста.
      let end = index + 1;
      while (end < sql.length) {
        if (sql[end] === char) {
          if (sql[end + 1] === char) {
            end += 2;
            continue;
          }
          break;
        }
        end += 1;
      }
      index = end + 1;
      continue;
    }
    if (char === '-' && next === '-') {
      const end = sql.indexOf('\n', index);
      index = end < 0 ? sql.length : end + 1;
      continue;
    }
    if (char === '/' && next === '*') {
      const end = sql.indexOf('*/', index + 2);
      index = end < 0 ? sql.length : end + 2;
      continue;
    }
    const before = index > 0 ? (sql[index - 1] ?? '') : '';
    if (char === '$') {
      const tag = /^\$([A-Za-z_][A-Za-z0-9_]*)?\$/.exec(sql.slice(index));
      if (tag && !/[A-Za-z0-9_$]/.test(before)) {
        const end = sql.indexOf(tag[0], index + tag[0].length);
        index = end < 0 ? sql.length : end + tag[0].length;
        continue;
      }
      const number = /^\$([1-9][0-9]*)/.exec(sql.slice(index));
      if (number && !/[A-Za-z0-9_$]/.test(before)) {
        add(number[0]);
        index += number[0].length;
        continue;
      }
    }
    if (char === '@') {
      const name = /^@([A-Za-z_][A-Za-z0-9_]*)/.exec(sql.slice(index));
      if (name && !/[A-Za-z0-9_@<]/.test(before)) {
        add(name[0]);
        index += name[0].length;
        continue;
      }
    }
    index += 1;
  }
  return found;
}

/** Выделение в кавычках одного вида (`"`, `'`, `` ` ``, `@"`) — без них (§5.13.23). */
export function stripQuotes(text: string): string {
  const trimmed = text.trim();
  if (trimmed.length >= 3 && trimmed.startsWith('@"') && trimmed.endsWith('"')) {
    return trimmed.slice(2, -1);
  }
  for (const quote of ['"', "'", '`']) {
    if (trimmed.length >= 2 && trimmed.startsWith(quote) && trimmed.endsWith(quote)) {
      return trimmed.slice(1, -1);
    }
  }
  return trimmed;
}
