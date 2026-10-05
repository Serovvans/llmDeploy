import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import { TitleHint } from './TitleHint';

function setup(cut: boolean) {
  render(
    <TitleHint text="Полное название документа">
      <span data-testid="name">Полное назв…</span>
    </TitleHint>,
  );
  const name = screen.getByTestId('name');
  Object.defineProperty(name, 'scrollWidth', { value: cut ? 300 : 100 });
  Object.defineProperty(name, 'clientWidth', { value: 100 });
  return name;
}

describe('подсказка с полным названием', () => {
  it('показывается при наведении на обрезанное название и скрывается, когда указатель ушёл', async () => {
    const user = userEvent.setup();
    const name = setup(true);

    await user.hover(name);
    expect(await screen.findByText('Полное название документа')).toBeInTheDocument();
    await user.unhover(name);
    expect(screen.queryByText('Полное название документа')).not.toBeInTheDocument();
  });

  it('не показывается, если название помещается целиком', async () => {
    const name = setup(false);
    await userEvent.setup().hover(name);
    expect(screen.queryByText('Полное название документа')).not.toBeInTheDocument();
  });
});
