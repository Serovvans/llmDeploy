import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import type { PortalConfig } from '../api/types';
import { CONFIG, mockApi, ok, session } from '../test/mockApi';
import { renderApp } from '../test/renderApp';
import { texts } from '../texts';

const tw = texts.workshop.wizard;
const CODE_CONFIG: PortalConfig = { ...CONFIG, code: { ...CONFIG.code, enabled: true } };

function nameInput(row: number): HTMLElement {
  const group = screen.getByRole('group', { name: `${tw.inputs}: ${row}` });
  return within(group).getAllByRole('textbox')[0] as HTMLElement;
}

describe('раздел «Код»: мастер «Новый GPTool»', () => {
  it('фокус: добавленный параметр — в его имя, удалённый — на соседнюю строку, затем на кнопку добавления (§9)', async () => {
    mockApi({
      'GET /api/config': () => ok(CODE_CONFIG),
      'GET /api/auth/session': () => ok(session('ready')),
      'GET /api/code/projects': () => ok({ items: [], next_cursor: null }),
    });
    renderApp('/code/new');
    const user = userEvent.setup();
    const add = await screen.findByRole('button', { name: tw.addInput }, { timeout: 5000 });

    await user.click(add);
    expect(nameInput(1)).toHaveFocus();
    await user.click(add);
    expect(nameInput(2)).toHaveFocus();

    await user.click(screen.getByRole('button', { name: tw.removeParam('1') }));
    expect(nameInput(1)).toHaveFocus();
    await user.click(screen.getByRole('button', { name: tw.removeParam('1') }));
    expect(add).toHaveFocus();
  });
});
