// Выставляет тему до первой отрисовки, чтобы не было вспышки чужой темы.
// Отдельный файл, а не встроенный код: встроенные скрипты запрещает политика содержимого.
// Ключ и значения совпадают с src/theme/preference.ts.
(function () {
  var preference = null;
  try {
    preference = window.localStorage.getItem('portal.theme');
  } catch {
    // Хранилище недоступно (закрытый режим браузера) — остаётся тема системы.
  }
  var dark =
    preference === 'dark' ||
    (preference !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  var theme = dark ? 'dark' : 'light';
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
})();
