/**
 * Итог проверки прав подключения (концепция §5.14, контракт §7.6.1): строки перечня лишних прав и текст для
 * администратора базы. Имена объектов — из базы заказчика: только текст.
 */
import type { AdminDbConnection, RightsFinding } from '../api/types';
import { texts } from '../texts';

const r = texts.db.rights;
/** Права без объектов: справа — их точное название. */
const ROLE_ATTRIBUTES = new Set(['superuser', 'createrole', 'createdb', 'replication', 'bypassrls']);

/** «a, b, c и ещё N» — первые три имени и сколько ещё (`total` — сколько всего). */
export function objectNames(finding: RightsFinding): string {
  const names = finding.objects.map((item) => item.name).join(', ');
  const more = finding.total - finding.objects.length;
  return more > 0 ? `${names} ${r.andMore(more)}` : names;
}

/** Строка перечня: слева — простыми словами, справа — точное название, которое ищет администратор базы. */
export function findingLine(finding: RightsFinding): { left: string; right: string } {
  const left = finding.kind === 'definer_function' ? r.definer : (r.kinds[finding.kind] ?? '');
  if (ROLE_ATTRIBUTES.has(finding.kind)) {
    return { left, right: finding.kind.toUpperCase() };
  }
  switch (finding.kind) {
    case 'write':
      return { left, right: `${finding.privileges.join(', ')}: ${objectNames(finding)}` };
    case 'create': {
      const created = finding.objects.map((item) => r.createObject(item.type, item.name)).join(', ');
      const more = finding.total - finding.objects.length;
      return { left, right: more > 0 ? `${created} ${r.andMore(more)}` : created };
    }
    case 'definer_function':
      return { left, right: `EXECUTE: ${objectNames(finding)}` };
    default:
      // Владение и служебные роли — имена объектов.
      return { left, right: objectNames(finding) };
  }
}

function clipboardLine(finding: RightsFinding): string {
  if (finding.kind === 'owner') {
    return r.clipboard.owner(objectNames(finding));
  }
  if (finding.kind === 'definer_function') {
    return r.clipboard.definer(objectNames(finding));
  }
  return `- ${findingLine(finding).right}`;
}

/** Текст «Скопировать список для администратора базы» (§5.14): без разметки. */
export function clipboardText(connection: AdminDbConnection): string {
  const { username, database, host, port, rights } = connection;
  const address = `${host}:${port}`;
  const warnings = rights.warnings.map(clipboardLine);
  if (rights.status === 'excess') {
    return [
      r.clipboard.excessHead(username, database, address),
      ...rights.findings.map(clipboardLine),
      ...(warnings.length > 0 ? [r.clipboard.alsoRemove, ...warnings] : []),
      r.clipboard.need,
    ].join('\n');
  }
  return [r.clipboard.warningHead(username, database, address), ...warnings].join('\n');
}

/** Причина «Не удалось подключиться» (§5.14): неизвестная — как «Прочее». */
export function reasonText(connection: AdminDbConnection): string {
  const error = connection.last_error;
  const reasons = r.reasons;
  switch (error?.reason) {
    case 'unreachable':
    case 'auth_failed':
    case 'tls_failed':
    case 'tls_required':
    case 'timeout':
    case 'busy':
      return reasons[error.reason];
    case 'database_not_found':
      return reasons.database_not_found(connection.database);
    case 'schemas_unavailable': {
      const names = error.details?.schemas;
      return reasons.schemas_unavailable(Array.isArray(names) ? names.map(String).join(', ') : '');
    }
    default:
      return reasons.other;
  }
}

/** Сколько лишних прав в итоге: предупреждения в счёт не входят. */
export function excessCount(connection: AdminDbConnection): number {
  return connection.rights.findings.length;
}
