import { describe, expect, it } from 'vitest';

import type { AdminDbConnection } from '../api/types';
import { texts } from '../texts';
import { parseExclusion, validateConnection } from './ConnectionForm';
import { clipboardText, findingLine, reasonText } from './rights';
import { availableAt, connectionOf, dbValue, refreshedAt } from './selection';

const r = texts.db.rights;

function connection(patch: Partial<AdminDbConnection> = {}): AdminDbConnection {
  return {
    id: 'c-1',
    title: 'Кадастр (рабочая)',
    description: '',
    host: 'db.mio.local',
    port: 5432,
    database: 'cadastre',
    username: 'portal_reader',
    tls_mode: 'verify',
    schemas: ['cadastre'],
    samples_allowed: false,
    sample_exclusions: [],
    access: { all: true },
    rights: { status: 'unknown', checked_at: null, findings: [], warnings: [] },
    last_error: null,
    snapshot: null,
    refresh_available_at: null,
    created_at: '2026-10-25T09:00:00Z',
    updated_at: '2026-10-25T09:00:00Z',
    ...patch,
  };
}

const OWNER = { kind: 'owner' as const, privileges: [], objects: [{ type: 'table', name: 'cadastre.parcels' }, { type: 'table', name: 'cadastre.leases' }], total: 14 };
const WRITE = { kind: 'write' as const, privileges: ['INSERT', 'UPDATE'], objects: [{ type: 'table', name: 'cadastre.parcels' }], total: 5 };
const DEFINER = {
  kind: 'definer_function' as const,
  privileges: ['EXECUTE'],
  objects: [{ type: 'function', name: 'cadastre.set_status' }, { type: 'function', name: 'cadastre.recalc_area' }],
  total: 4,
};

describe('итог проверки прав', () => {
  it('строки перечня: простыми словами и точное название с первыми именами', () => {
    expect(findingLine({ kind: 'superuser', privileges: [], objects: [], total: 0 })).toEqual({ left: r.kinds.superuser, right: 'SUPERUSER' });
    expect(findingLine(OWNER).right).toBe('cadastre.parcels, cadastre.leases и ещё 12');
    expect(findingLine(WRITE).right).toBe('INSERT, UPDATE: cadastre.parcels и ещё 4');
    expect(findingLine({ kind: 'create', privileges: ['CREATE'], objects: [{ type: 'database', name: 'cadastre' }, { type: 'schema', name: 'public' }], total: 2 }).right).toBe(
      'CREATE: база cadastre, CREATE: схема public',
    );
    expect(findingLine(DEFINER)).toEqual({ left: r.definer, right: 'EXECUTE: cadastre.set_status, cadastre.recalc_area и ещё 2' });
  });

  it('текст для администратора базы: лишние права, «желательно снять» — только с предупреждением', () => {
    const excess = connection({ rights: { status: 'excess', checked_at: null, findings: [{ kind: 'superuser', privileges: [], objects: [], total: 0 }, OWNER, WRITE], warnings: [DEFINER] } });
    expect(clipboardText(excess)).toBe(
      [
        'Учётная запись portal_reader базы cadastre (db.mio.local:5432) имеет права,',
        'с которыми портал сотрудников с базой не работает:',
        '- SUPERUSER',
        '- владелец: cadastre.parcels, cadastre.leases и ещё 12',
        '- INSERT, UPDATE: cadastre.parcels и ещё 4',
        'Желательно снять и это право (портал работает и с ним):',
        '- EXECUTE на функции SECURITY DEFINER: cadastre.set_status, cadastre.recalc_area и ещё 2',
        'Порталу нужна роль только для чтения: LOGIN, CONNECT к базе, USAGE на нужные',
        'схемы, SELECT на их таблицы, default_transaction_read_only = on.',
      ].join('\n'),
    );
    expect(clipboardText({ ...excess, rights: { ...excess.rights, warnings: [] } })).not.toContain('Желательно');

    const warningOnly = connection({ rights: { status: 'read_only', checked_at: null, findings: [], warnings: [DEFINER] } });
    expect(clipboardText(warningOnly).split('\n')).toEqual([
      'Учётная запись portal_reader базы cadastre (db.mio.local:5432) только читает,',
      'но может вызывать функции, которые пишут от имени их владельца. Портал их',
      'не вызывает; желательно снять право:',
      '- EXECUTE на функции SECURITY DEFINER: cadastre.set_status, cadastre.recalc_area и ещё 2',
    ]);
  });

  it('причина сбоя: имя базы и схем из ответа; неизвестная — как «Прочее»', () => {
    const at = '2026-10-26T09:00:00Z';
    expect(reasonText(connection({ last_error: { reason: 'database_not_found', details: null, at } }))).toBe(r.reasons.database_not_found('cadastre'));
    expect(reasonText(connection({ last_error: { reason: 'schemas_unavailable', details: { schemas: ['a', 'b'] }, at } }))).toBe(
      r.reasons.schemas_unavailable('a, b'),
    );
    expect(reasonText(connection({ last_error: { reason: 'new_reason', details: null, at } }))).toBe(r.reasons.other);
  });
});

describe('форма подключения', () => {
  const VALID = {
    title: 'Кадастр',
    description: '',
    host: 'db.mio.local',
    port: '5432',
    database: 'cadastre',
    username: 'portal_reader',
    password: 'secret',
    tls_mode: 'verify' as const,
    schemas: ['cadastre'],
    samples_allowed: true,
    exclusions: ['cadastre.owners', 'cadastre.contacts.phone'],
    accessAll: true,
    users: [],
  };

  it('правильная форма — без ошибок; пароль обязателен только у нового', () => {
    expect(validateConnection(VALID, true)).toEqual({});
    expect(validateConnection({ ...VALID, password: '' }, true).password).toBe(texts.db.form.errors.password);
    expect(validateConnection({ ...VALID, password: '' }, false)).toEqual({});
  });

  it('ошибки по полям', () => {
    const e = texts.db.form.errors;
    const errors = validateConnection(
      { ...VALID, title: ' ', port: '70000', schemas: [], exclusions: ['owners'], accessAll: false, users: [] },
      true,
    );
    expect(errors).toMatchObject({ title: e.title, port: e.port, schemas: e.schemas, exclusions: e.exclusionFormat, access: e.access });
    expect(validateConnection({ ...VALID, exclusions: ['public.t'] }, true).exclusions).toBe(e.exclusionSchema('public'));
    // Исключения без разрешённых образцов не проверяются: они сохраняются как есть.
    expect(validateConnection({ ...VALID, samples_allowed: false, exclusions: ['bad'] }, true).exclusions).toBeUndefined();
    expect(validateConnection({ ...VALID, schemas: ['a\u0001'] }, true).schemas).toBe(e.schemaInvalid('a\u0001'));
  });

  it('исключение — схема.таблица или схема.таблица.столбец', () => {
    expect(parseExclusion('cadastre.owners')).toEqual({ schema: 'cadastre', table: 'owners', column: null });
    expect(parseExclusion('cadastre.contacts.phone')).toEqual({ schema: 'cadastre', table: 'contacts', column: 'phone' });
    expect(parseExclusion('owners')).toBeNull();
    expect(parseExclusion('a..b')).toBeNull();
  });
});

describe('выбор базы', () => {
  it('значение списка у подключения отличается от идентификатора схемы', () => {
    expect(connectionOf(dbValue('c-1'))).toBe('c-1');
    expect(connectionOf('s-1')).toBeNull();
    expect(connectionOf(null)).toBeNull();
  });

  it('время обновления схемы: сегодня — только время', () => {
    const now = new Date(2026, 9, 26, 15, 0);
    expect(refreshedAt(new Date(2026, 9, 26, 14, 5).toISOString(), now)).toBe('сегодня в 14:05');
    expect(availableAt(new Date(2026, 9, 26, 14, 35).toISOString(), now)).toBe('14:35');
    expect(availableAt(new Date(2026, 9, 27, 9, 0).toISOString(), now)).toBe('27.10.2026, 09:00');
  });
});
