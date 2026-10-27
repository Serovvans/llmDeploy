import '@testing-library/jest-dom/vitest';

import { cleanup, configure } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';

import { setSystemDark } from './systemTheme';

// Screen tests boot the whole app (session, settings, data) before the first assertion; on a loaded machine that
// takes longer than the default 1 s of `findBy`/`waitFor`. The limit is only a ceiling: passing tests do not wait
// longer, and no assertion depends on it.
configure({ asyncUtilTimeout: 5000 });

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
