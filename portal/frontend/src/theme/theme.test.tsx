import { act, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';

import themeInitScript from '../../public/theme-init.js?raw';
import { mockApi, ok, session } from '../test/mockApi';
import { menuItems, renderApp } from '../test/renderApp';
import { setSystemDark } from '../test/systemTheme';
import { texts } from '../texts';
import { readPreference, resolveTheme, storePreference, THEME_STORAGE_KEY } from './preference';

const root = document.documentElement;

function runThemeInit(): void {
  new Function(themeInitScript)();
}

async function chooseTheme(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getByRole('button', { name: texts.nav.theme }));
  await user.click(await screen.findByText(label));
}

describe('выбор темы', () => {
  it('по умолчанию — как в системе; выбор хранится под ключом portal.theme', () => {
    expect(THEME_STORAGE_KEY).toBe('portal.theme');
    expect(readPreference()).toBe('system');

    storePreference('dark');
    expect(window.localStorage.getItem('portal.theme')).toBe('dark');
    expect(readPreference()).toBe('dark');

    storePreference('system');
    expect(window.localStorage.getItem('portal.theme')).toBeNull();

    window.localStorage.setItem('portal.theme', 'sepia');
    expect(readPreference()).toBe('system');
  });

  it('«Как в системе» следует системе, явный выбор её перекрывает', () => {
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('light', true)).toBe('light');
    expect(resolveTheme('dark', false)).toBe('dark');
  });
});

describe('скрипт установки темы до отрисовки', () => {
  it.each([
    [null, false, 'light'],
    [null, true, 'dark'],
    ['dark', false, 'dark'],
    ['light', true, 'light'],
  ] as const)('выбор %s при тёмной системе=%s даёт тему %s', (stored, systemDark, expected) => {
    if (stored) {
      window.localStorage.setItem('portal.theme', stored);
    }
    setSystemDark(systemDark);

    runThemeInit();

    expect(root.dataset.theme).toBe(expected);
    expect(root.style.colorScheme).toBe(expected);
  });
});

describe('темы в приложении', () => {
  it('без выбора следует системе и меняется вместе с ней без перезагрузки', async () => {
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    setSystemDark(true);
    renderApp('/chat');
    await screen.findByRole('navigation');

    expect(root.dataset.theme).toBe('dark');
    expect(root.style.colorScheme).toBe('dark');

    act(() => setSystemDark(false));
    expect(root.dataset.theme).toBe('light');
  });

  it('переключатель в рейке: выбор применяется сразу, запоминается и отмечен галочкой', async () => {
    const user = userEvent.setup();
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/chat');
    await screen.findByRole('navigation');
    expect(root.dataset.theme).toBe('light');

    await chooseTheme(user, texts.nav.themeDark);
    expect(root.dataset.theme).toBe('dark');
    expect(root.style.colorScheme).toBe('dark');
    expect(window.localStorage.getItem('portal.theme')).toBe('dark');

    // Системная тема больше не влияет.
    act(() => setSystemDark(true));
    act(() => setSystemDark(false));
    expect(root.dataset.theme).toBe('dark');

    await user.click(screen.getByRole('button', { name: texts.nav.theme }));
    await screen.findByText(texts.nav.themeSystem);
    const items = menuItems();
    expect(items.map((item) => item.textContent)).toEqual(['Как в системе', 'Светлая', 'Тёмная, выбрана']);
    expect(items.map((item) => item.querySelector('svg') !== null)).toEqual([false, false, true]);
    await user.click(within(items[0] as HTMLElement).getByText(texts.nav.themeSystem));

    await waitFor(() => expect(root.dataset.theme).toBe('light'));
    expect(window.localStorage.getItem('portal.theme')).toBeNull();
  });

  it('выбранная раньше тема действует на экране входа, переключателя там нет', async () => {
    window.localStorage.setItem('portal.theme', 'dark');
    mockApi({ 'GET /api/auth/session': () => ({ status: 401, body: { error: { code: 'unauthenticated', message: '' } } }) });
    renderApp('/login');

    await screen.findByLabelText(texts.login.loginLabel);
    expect(root.dataset.theme).toBe('dark');
    expect(screen.queryByText(texts.nav.theme)).not.toBeInTheDocument();
  });

  it.each(['light', 'dark'] as const)('переменные портала выставлены из токенов темы: %s', async (theme) => {
    window.localStorage.setItem('portal.theme', theme);
    mockApi({ 'GET /api/auth/session': () => ok(session('ready')) });
    renderApp('/chat');
    await screen.findByRole('navigation');

    for (const name of [
      '--p-bg',
      '--p-text',
      '--p-text-muted',
      '--p-surface-2',
      '--p-surface-3',
      '--p-line',
      '--p-accent',
      '--p-focus',
      '--p-error-text',
      '--p-error-bg',
      '--p-warning-bg',
      '--p-warning-icon',
      '--p-mark-bg',
    ]) {
      expect(root.style.getPropertyValue(name), name).not.toBe('');
    }
    expect(root.style.getPropertyValue('--p-bg')).toContain(theme === 'dark' ? '#333333' : '#ffffff');

    // Состояния — смеси с одним правилом на обе темы; светлые токены *Secondary не используются.
    const value = (name: string) => root.style.getPropertyValue(name);
    expect(value('--p-error-bg')).toMatch(/^color-mix\(in srgb, #FE4C4C 20%, /);
    expect(value('--p-warning-bg')).toMatch(/^color-mix\(in srgb, #(fcb73e|ffa236) 20%, /);
    expect(value('--p-error-text')).toMatch(/^color-mix\(in srgb, .+ 70%, /);
    expect(value('--p-warning-icon')).toMatch(/^color-mix\(in srgb, #(fcb73e|ffa236) 55%, /);
    expect(value('--p-error-bg') + value('--p-warning-bg')).not.toMatch(/FFEBEB|fff0bc/i);
  });
});
