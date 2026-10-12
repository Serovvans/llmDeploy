import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PageHeader } from './PageHeader';
import styles from './PageHeader.module.css';

const ROW_WIDTH = 400;
const WIDTHS: Record<string, number> = { settings: 260, actions: 48 };

/** Раскладку jsdom не считает: ширины задаются по отметкам элементов, ширина строки — постоянная. */
function stubLayout() {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockReturnValue(ROW_WIDTH);
  vi.spyOn(HTMLElement.prototype, 'offsetWidth', 'get').mockImplementation(function (this: HTMLElement) {
    const marked = this.matches('[data-width]') ? this : this.querySelector<HTMLElement>('[data-width]');
    return WIDTHS[marked?.dataset.width ?? ''] ?? 0;
  });
}

const settings = <span data-width="settings">Настройки</span>;

describe('строка заголовка с настройками', () => {
  afterEach(() => vi.restoreAllMocks());

  it('появление действий пересчитывает перенос настроек, хотя размеры строки не менялись', async () => {
    stubLayout();
    const { rerender } = render(<PageHeader title="Запрос" settings={settings} />);
    const header = screen.getByRole('banner');
    // 120 (название) + 260 (настройки) помещаются в 400.
    expect(header).not.toHaveClass(styles.wrapped as string);

    rerender(
      <PageHeader title="Запрос" settings={settings}>
        <span data-width="actions">Действия</span>
      </PageHeader>,
    );
    // 120 + 260 + 48 — уже нет: настройки уходят на вторую строку, действия остаются на первой.
    await waitFor(() => expect(header).toHaveClass(styles.wrapped as string));
    expect(screen.getByText('Действия')).toBeInTheDocument();
  });

  it('на время правки названия настройки остаются — на второй строке', () => {
    render(<PageHeader title="Запрос" settings={settings} editor={<input aria-label="Название" />} />);
    expect(screen.getByRole('banner')).toHaveClass(styles.wrapped as string);
    expect(screen.getByLabelText('Название')).toBeInTheDocument();
    expect(screen.getByText('Настройки')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1 })).not.toBeInTheDocument();
  });
});
