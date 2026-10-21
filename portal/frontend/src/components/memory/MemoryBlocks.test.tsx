import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { texts } from '../../texts';
import { SummaryBlock } from './MemoryBlocks';

const reason = texts.memory.summary.nothing(2);

describe('«Сжать раннюю часть», когда сжимать нечего', () => {
  it('остаётся в обходе по Tab, объявляет причину и показывает её при переходе с клавиатуры', async () => {
    const user = userEvent.setup();
    render(
      <SummaryBlock
        summary={null}
        range={null}
        compressPairs={null}
        keepRecent={2}
        busy={false}
        onCompress={() => undefined}
        onEdit={() => undefined}
        onCancel={() => undefined}
      />,
    );
    const button = screen.getByRole('button', { name: texts.memory.summary.compress });
    expect(button).toHaveAccessibleDescription(reason);

    await user.tab();
    expect(button).toHaveFocus();
    // The hidden description plus the popup copy.
    expect(screen.getAllByText(reason)).toHaveLength(2);
    await user.tab();
    expect(screen.getAllByText(reason)).toHaveLength(1);
  });
});
