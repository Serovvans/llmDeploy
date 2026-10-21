import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { DialogMemory } from '../../memory/useDialogMemory';
import { texts } from '../../texts';
import { MemoryMeter } from './MemoryMeter';

const memory: DialogMemory = {
  active: true,
  status: 'new',
  memory: null,
  level: 'free',
  truncated: false,
  rowErrors: {},
  announcement: '',
  retry: () => undefined,
  setMode: () => undefined,
  markTruncated: () => undefined,
  refresh: () => undefined,
  disable: () => undefined,
};
const hint = texts.memory.percentHint(0);

/** The hint is also in a visually hidden span for `aria-describedby`; count only the popup copy. */
const shownCopies = () => screen.queryAllByText(hint, { normalizer: (text) => text }).length - 1;

describe('подсказка индикатора памяти', () => {
  it('открывается при переходе на индикатор с клавиатуры и закрывается при уходе фокуса', async () => {
    const user = userEvent.setup();
    render(<MemoryMeter memory={memory} panelId="panel" panelOpen={false} onOpen={() => undefined} />);

    await user.tab();
    expect(screen.getByRole('button')).toHaveFocus();
    expect(shownCopies()).toBe(1);
    await user.tab();
    expect(shownCopies()).toBe(0);
  });
});
