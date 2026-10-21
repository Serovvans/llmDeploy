import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { api } from '../api/client';
import { draftNotice } from './draft';
import { useSummaryDraft } from './useSummaryDraft';

afterEach(() => vi.restoreAllMocks());

describe('поток черновика пересказа', () => {
  it('неизвестное исключение переводит черновик в ошибку, а не оставляет «streaming»', async () => {
    // eslint-disable-next-line require-yield
    vi.spyOn(api, 'draftSummary').mockImplementation(async function* () {
      throw new TypeError('непредвиденное');
    });
    const { result } = renderHook(() => useSummaryDraft('d1', vi.fn()));
    act(() => result.current.start({ purpose: 'compress' }));
    await waitFor(() => expect(result.current.state.phase).toBe('failed'));
    const failure = result.current.state.failure;
    expect(failure && draftNotice(failure, false, { purpose: 'compress', keepRecent: 2, verbatim: false })?.action).toBe('restart');
  });
});
