import '@testing-library/jest-dom/vitest';

import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

import { setSystemDark } from './systemTheme';

beforeEach(() => {
  setSystemDark(false);
  // Анимации библиотеки в jsdom не нужны, а прокрутки к элементу в нём нет.
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('style');
  vi.unstubAllGlobals();
});
