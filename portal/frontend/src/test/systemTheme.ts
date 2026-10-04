import { vi } from 'vitest';

type Listener = (event: MediaQueryListEvent) => void;

let listeners = new Set<Listener>();
let dark = false;

/** Подменяет `matchMedia`: в jsdom его нет. Меняет системную тему и оповещает подписчиков. */
export function setSystemDark(next: boolean): void {
  const changed = dark !== next;
  dark = next;
  if (!vi.isMockFunction(window.matchMedia)) {
    listeners = new Set();
    window.matchMedia = vi.fn(
      (query: string) =>
        ({
          get matches() {
            return dark;
          },
          media: query,
          addEventListener: (_type: string, listener: Listener) => listeners.add(listener),
          removeEventListener: (_type: string, listener: Listener) => listeners.delete(listener),
        }) as unknown as MediaQueryList,
    );
  }
  if (changed) {
    listeners.forEach((listener) => listener({ matches: dark } as MediaQueryListEvent));
  }
}
