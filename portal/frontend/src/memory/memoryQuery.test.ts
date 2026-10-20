import { describe, expect, it } from 'vitest';

import { memoryQuery } from './useDialogMemory';

describe('строка запроса оценки памяти', () => {
  it('берёт только то, от чего зависит место под разговор (контракт §5.10.3)', () => {
    expect(memoryQuery({ mode: 'thorough', collections: ['shared'], tags: ['аренда'] })).toEqual({ mode: 'thorough', search: 'true' });
    expect(memoryQuery({ mode: 'fast', collections: [] })).toEqual({ mode: 'fast', search: 'false' });
    // Проверка на тестовых данных место не меняет; «Без схемы» — без `schema_id`.
    expect(memoryQuery({ action: 'explain', dialect: 'mssql', schema_id: 's-1', test_run: true })).toEqual({
      action: 'explain',
      dialect: 'mssql',
      schema_id: 's-1',
    });
    expect(memoryQuery({ action: 'write', dialect: 'postgres', schema_id: null, test_run: false })).toEqual({
      action: 'write',
      dialect: 'postgres',
    });
    expect(memoryQuery({ action: 'debug' })).toEqual({ action: 'debug' });
    expect(memoryQuery({})).toEqual({});
  });
});
