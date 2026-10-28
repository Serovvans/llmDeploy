import { describe, expect, it } from 'vitest';

import { ApiError } from '../api/client';
import type { AdminDbConnection, DbSnapshot } from '../api/types';
import { texts } from '../texts';
import { missingExclusions, parseExclusion, serverErrors, validateConnection } from './ConnectionForm';
import { dbCheckUnavailable, dbRefusal } from './refusal';
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
    all_schemas: false,
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

  it('виды 0.25: права из находки, у выдачи ролей — своя строка для копирования', () => {
    const maintain = { kind: 'maintain' as const, privileges: ['MAINTAIN'], objects: [{ type: 'table', name: 'cadastre.parcels' }], total: 4 };
    const grant = {
      kind: 'grant_option' as const,
      privileges: ['SELECT', 'USAGE'],
      objects: [{ type: 'table', name: 'cadastre.parcels' }, { type: 'schema', name: 'cadastre' }],
      total: 3,
    };
    const admin = { kind: 'admin_option' as const, privileges: [], objects: [{ type: 'role', name: 'app_ro' }, { type: 'role', name: 'gis_editors' }], total: 2 };
    const settings = {
      kind: 'server_settings' as const,
      privileges: ['ALTER SYSTEM', 'SET'],
      objects: [{ type: 'parameter', name: 'work_mem' }, { type: 'parameter', name: 'log_statement' }],
      total: 4,
    };
    expect(findingLine(maintain)).toEqual({ left: r.kinds.maintain, right: 'MAINTAIN: cadastre.parcels и ещё 3' });
    expect(findingLine(grant)).toEqual({ left: r.kinds.grant_option, right: 'SELECT, USAGE WITH GRANT OPTION: cadastre.parcels, cadastre и ещё 1' });
    expect(findingLine(admin)).toEqual({ left: r.kinds.admin_option, right: 'WITH ADMIN OPTION: app_ro, gis_editors' });
    expect(findingLine(settings)).toEqual({ left: r.kinds.server_settings, right: 'ALTER SYSTEM, SET ON PARAMETER: work_mem, log_statement и ещё 2' });
    const excess = connection({ rights: { status: 'excess', checked_at: null, findings: [maintain, grant, admin, settings], warnings: [] } });
    expect(clipboardText(excess).split('\n').slice(2, 6)).toEqual([
      '- MAINTAIN: cadastre.parcels и ещё 3',
      '- SELECT, USAGE WITH GRANT OPTION: cadastre.parcels, cadastre и ещё 1',
      '- членство WITH ADMIN OPTION в ролях: app_ro, gis_editors',
      '- ALTER SYSTEM, SET ON PARAMETER: work_mem, log_statement и ещё 2',
    ]);
  });

  it('виды решения 44: служебные функции, большие объекты, отключённая проверка прав на них', () => {
    const functions = { kind: 'catalog_function' as const, privileges: ['EXECUTE'], objects: [{ type: 'function', name: 'lo_export(oid,text)' }, { type: 'function', name: 'public.dblink_connect_u(text)' }], total: 3 };
    const objects = {
      kind: 'large_object' as const,
      privileges: ['UPDATE'],
      objects: [{ type: 'large object', name: '16403' }, { type: 'large object', name: '16404' }],
      total: 9,
    };
    const compat = { kind: 'lo_compat_privileges' as const, privileges: [], objects: [{ type: 'parameter', name: 'lo_compat_privileges' }], total: 1 };
    expect(findingLine(functions)).toEqual({ left: r.kinds.catalog_function, right: 'EXECUTE ON FUNCTION: lo_export(oid,text), public.dblink_connect_u(text) и ещё 1' });
    expect(findingLine(objects)).toEqual({ left: r.kinds.large_object, right: 'UPDATE ON LARGE OBJECT: 16403, 16404 и ещё 7' });
    expect(findingLine(compat)).toEqual({ left: r.kinds.lo_compat_privileges, right: 'lo_compat_privileges = on' });
    const excess = connection({ rights: { status: 'excess', checked_at: null, findings: [functions, objects, compat], warnings: [] } });
    expect(clipboardText(excess).split('\n').slice(2, 5)).toEqual([
      '- EXECUTE ON FUNCTION: lo_export(oid,text), public.dblink_connect_u(text) и ещё 1',
      '- UPDATE ON LARGE OBJECT: 16403, 16404 и ещё 7',
      '- lo_compat_privileges = on в сеансе учётной записи (проверка прав на большие объекты отключена)',
    ]);
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
    expect(reasonText(connection({ last_error: { reason: 'host_forbidden', details: null, at } }))).toBe(r.reasons.host_forbidden);
    expect(reasonText(connection({ last_error: { reason: 'new_reason', details: null, at } }))).toBe(r.reasons.other);
  });

  it('внутренний адрес портала: у администратора — своя причина, у сотрудника — прежний общий текст', () => {
    const error = new ApiError(503, { code: 'db_unavailable', message: '', details: { reason: 'host_forbidden' } });
    expect(dbRefusal(error)).toBe(texts.db.refusals.connect);
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
    all_schemas: false,
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

  it('1.0.1: при «Все схемы» список не проверяется, схема исключения — любая', () => {
    const all = { ...VALID, all_schemas: true, schemas: [], exclusions: ['archive.notes', 'Zeta.t.c'] };
    expect(validateConnection(all, true)).toEqual({});
    expect(validateConnection({ ...all, exclusions: ['notes'] }, true).exclusions).toBe(texts.db.form.errors.exclusionFormat);
  });

  it('1.0.1: «Нет в загруженной схеме» — точное имя таблицы и столбца', () => {
    const snapshot: DbSnapshot = {
      connection_id: 'c-1',
      taken_at: '2026-10-28T09:12:00Z',
      truncated: false,
      tables: [
        {
          schema: 'cadastre',
          name: 'owners',
          kind: 'table',
          comment: null,
          samples_excluded: true,
          columns: [{ name: 'phone', type: 'text', not_null: false, primary_key: false, references: null, comment: null, geometry: null, excluded: true }],
        },
      ],
    };
    expect(
      missingExclusions(['cadastre.owners', 'cadastre.owners.phone', 'Cadastre.owners', 'cadastre.owners.mail', 'cadastre.ownres', 'bad'], snapshot),
    ).toEqual(['Cadastre.owners', 'cadastre.owners.mail', 'cadastre.ownres']);
  });

  it('ошибка сервера по полю «Адрес»: внутренний адрес портала — свой текст', () => {
    const e = texts.db.form.errors;
    const refused = (code: string) => new ApiError(422, { code: 'validation_error', message: '', fields: [{ field: 'host', code, message: '' }] });
    expect(serverErrors(refused('host_forbidden'), VALID)).toEqual({ host: e.hostForbidden });
    expect(serverErrors(refused('required'), VALID)).toEqual({ host: e.host });
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

describe('«Проверить запрос»: отказ подключения — строка «Проверка недоступна» (§5.13.24)', () => {
  const reasons = texts.db.run.reasons;
  const error = (code: string, details?: Record<string, unknown>) => new ApiError(409, { code, message: '', ...(details ? { details } : {}) });

  it('коды подключения — однострочное состояние; прочие отказы — не оно', () => {
    expect(dbCheckUnavailable(error('db_connection_unavailable'))).toBe(reasons.connection_unavailable);
    expect(dbCheckUnavailable(error('db_connection_unusable'))).toBe(reasons.connection_unusable);
    expect(dbCheckUnavailable(error('db_unavailable', { reason: 'busy' }))).toBe(reasons.db_busy);
    expect(dbCheckUnavailable(error('db_unavailable', { reason: 'timeout' }))).toBe(reasons.db_unavailable);
    expect(dbCheckUnavailable(error('db_samples_not_allowed'))).toBeNull();
    expect(dbCheckUnavailable(new Error('x'))).toBeNull();
  });
});
