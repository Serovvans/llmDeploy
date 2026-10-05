/** Значение, которое обязано быть: иначе тест падает с понятной причиной, а не на `undefined`. */
export function must<T>(value: T | null | undefined, what: string): T {
  if (value === null || value === undefined) {
    throw new Error(`Нет значения: ${what}`);
  }
  return value;
}
