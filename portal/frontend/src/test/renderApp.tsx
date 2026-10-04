import { render } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { App } from '../App';

function CurrentPath() {
  return <span data-testid="path">{useLocation().pathname}</span>;
}

/** Отрисовывает приложение целиком по адресу `path`; текущий адрес — в элементе `path`. */
export function renderApp(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
      <CurrentPath />
    </MemoryRouter>,
  );
}

/** Подписи пунктов открытого меню библиотеки (у её пунктов нет роли `menuitem`). */
export function menuItems(): HTMLElement[] {
  return Array.from(document.querySelectorAll<HTMLElement>('[data-tid="MenuItem__root"]'));
}
