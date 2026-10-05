/**
 * Состояния интерфейса на живом стенде (docs/portal-ui.md §6): сбои модели, обрыв потока,
 * потеря связи, завершение сеанса, отказ в доступе, «не найдено», пустые состояния.
 * Сбои модели вызываются пометками заглушки (docs/portal-api.md §12.2).
 */
import type { Page } from '@playwright/test';

import { nextCode, uniqueSuffix } from '../support/accounts';
import { textFile } from '../support/files';
import { expect, test } from '../support/fixtures';
import { must } from '../support/must';
import { addKbDocument, chatWithAnswer, createDialog, expectError } from '../support/portal';
import { composer, expectAnswerReady, expectChatConfigLoaded, fillLogin, send } from '../support/ui';

const MISSING = '00000000-0000-4000-8000-000000000000';

function answerAlert(page: Page) {
  return page.locator('main').getByRole('alert');
}

test.describe('Сбои модели', () => {
  const cases = [
    { mark: 'error', title: 'модель недоступна', text: 'Не удалось получить ответ. Попробуйте ещё раз' },
    { mark: 'overloaded', title: 'модель занята', text: 'Модель сейчас занята. Повторите через минуту' },
    { mark: 'break', title: 'поток оборвался посреди ответа', text: 'Ответ оборвался. Можно повторить запрос' },
    {
      mark: 'context-overflow',
      title: 'запрос не помещается в память модели',
      text: 'Сообщение не помещается в память модели. Сократите текст или прикрепите документ меньшего объёма',
    },
  ];

  for (const { mark, title, text } of cases) {
    test(`${title}: понятный текст и «Повторить»`, async ({ newMember, pageAs }) => {
      const member = await newMember();
      const page = await pageAs(member);
      const question = `Вопрос со сбоем [[stub:${mark}]]`;
      await send(page, question);

      await expect(answerAlert(page)).toContainText(text);
      // Вопрос не потерян, поле ввода снова доступно.
      await expect(page.getByRole('paragraph').filter({ hasText: question })).toBeVisible();
      await expect(page.getByRole('button', { name: 'Отправить', exact: true })).toBeVisible();

      const retry = page.waitForRequest((request) => request.url().endsWith('/regenerate'));
      await answerAlert(page).getByRole('button', { name: 'Повторить' }).click();
      await retry;
      await expect(answerAlert(page)).toContainText(text);

      // Диалог со сбоем остаётся рабочим: следующий вопрос получает ответ.
      await send(page, 'Обычный вопрос после сбоя');
      await expectAnswerReady(page);
    });
  }

  test('оборванный ответ сохраняет полученную часть текста', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await send(page, 'Начни и оборвись [[stub:break]]');
    await expect(answerAlert(page)).toContainText('Ответ оборвался');
    await expect(page.locator('main')).toContainText('Заглушка. Вопрос:');

    const dialogId = must(page.url().split('/').pop(), 'идентификатор чата в адресе');
    const messages = await (await member.api.get(`/api/dialogs/${dialogId}/messages`)).json();
    expect(messages.items[0]).toMatchObject({ role: 'assistant', status: 'error', error_code: 'model_unavailable' });
    expect(messages.items[0].content.length).toBeGreaterThan(0);
  });

  test('ответ упёрся в предельную длину: заметка с подсказкой, ошибки нет', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await send(page, 'Очень длинный ответ [[stub:length]]');
    await expect(
      page.getByText('Ответ достиг предельной длины и оборвался. Напишите „продолжай“, чтобы получить остальное'),
    ).toBeVisible();
    await expect(answerAlert(page)).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Ответить заново' })).toBeVisible();
  });
});

test.describe('Связь и сеанс', () => {
  test('связь пропала перед отправкой: текст о связи, вопрос остаётся в поле', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

    await page.context().setOffline(true);
    await composer(page).fill('Вопрос без связи');
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expect(page.getByText(/Пропала связь с порталом/).first()).toBeVisible();
    await expect(composer(page)).toHaveValue('Вопрос без связи');

    await page.context().setOffline(false);
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expectAnswerReady(page);
  });

  test('настройки интерфейса не загрузились: в чате вопрос уходит без них', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

    // Сессия загружается, настройки интерфейса — нет: пределы проверит сервер.
    await page.route('**/api/config', (route) => route.abort('connectionreset'));
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Прикрепить' })).toBeEnabled();

    await send(page, 'Вопрос без настроек интерфейса');
    await expectAnswerReady(page);
    await expect(page.locator('main')).toContainText('Заглушка. Вопрос: «Вопрос без настроек интерфейса»');
  });

  test('настройки интерфейса не загрузились: SQL-помощник объясняет отказ, после повтора работает', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

    await page.route('**/api/config', (route) => route.abort('connectionreset'));
    await page.goto('/sql');
    const failed = 'Не удалось загрузить настройки портала';
    const toolbarNotice = page.getByRole('alert').filter({ has: page.getByRole('button', { name: 'Повторить' }) });
    await expect(toolbarNotice).toHaveText(new RegExp(`^${failed}`));
    // Без настроек нечего показать на месте списка диалектов.
    await expect(page.getByRole('button', { name: 'Диалект' })).toHaveCount(0);

    // Без диалекта вопрос не уходит, но отказ объяснён, а набранное остаётся в поле.
    const field = composer(page, /Опишите словами, что нужно получить/);
    await field.fill('Участки площадью больше гектара');
    let sent = 0;
    page.on('request', (request) => {
      if (/\/api\/dialogs/.test(request.url()) && request.method() === 'POST') {
        sent += 1;
      }
    });
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expect(page.getByRole('alert').filter({ hasText: failed })).toHaveCount(2);
    await expect(field).toHaveValue('Участки площадью больше гектара');
    expect(sent).toBe(0);

    // Связь вернулась: «Повторить» загружает настройки, заметка и строка исчезают без сообщения.
    await page.unroute('**/api/config');
    await toolbarNotice.getByRole('button', { name: 'Повторить' }).click();
    await expect(page.getByRole('button', { name: 'Диалект' })).toContainText('PostgreSQL + PostGIS');
    await expect(page.getByText(failed)).toHaveCount(0);
    await expect(field).toHaveValue('Участки площадью больше гектара');

    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expectAnswerReady(page);
  });

  test('настройки интерфейса загружаются сами, без действий сотрудника', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

    // Первый запрос настроек обрывается, следующие проходят.
    let failures = 1;
    await page.route('**/api/config', (route) => {
      if (failures > 0) {
        failures -= 1;
        return route.abort('connectionreset');
      }
      return route.continue();
    });
    await page.goto('/documents');
    await expect(page.getByRole('alert').filter({ hasText: 'Не удалось загрузить настройки портала' })).toBeVisible();
    await expect(page.getByRole('radio')).toHaveCount(0);

    // Повтор идёт сам каждые несколько секунд: шаблоны появляются, заметка исчезает.
    await expect(page.getByRole('radio')).toHaveCount(4, { timeout: 15_000 });
    await expect(page.getByText('Не удалось загрузить настройки портала')).toHaveCount(0);
  });

  test('связь пропала при входе: текст о связи вместо «неверный пароль»', async ({ newMember, page }) => {
    const { account } = await newMember();
    await page.goto('/login');
    await expect(page.getByRole('button', { name: 'Войти' })).toBeVisible();
    await page.context().setOffline(true);
    await fillLogin(page, account.login, account.password);
    await expect(page.getByRole('alert')).toHaveText(
      'Не удалось связаться с порталом. Проверьте подключение и попробуйте снова',
    );
    await page.context().setOffline(false);
    await page.getByRole('button', { name: 'Войти' }).click();
    await expect(page.getByRole('heading', { name: 'Код из приложения' })).toBeVisible();
  });

  test('портал не отвечает при открытии: экран «Портал не отвечает» и повтор', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();

    // Запрос сессии обрывается на уровне сети браузера — так выглядит недоступный портал.
    await page.route('**/api/auth/session', (route) => route.abort('connectionrefused'));
    await page.reload();
    await expect(page.getByRole('heading', { name: 'Портал не отвечает' })).toBeVisible();
    await expect(
      page.getByText('Проверьте подключение к сети. Если с ним всё в порядке — попробуйте через несколько минут'),
    ).toBeVisible();

    await page.unroute('**/api/auth/session');
    await page.getByRole('button', { name: 'Попробовать снова' }).click();
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
  });

  test('сеанс завершён в другом месте: переход на вход с пояснением и возврат на тот же экран', async ({
    newMember,
    pageAs,
  }) => {
    const member = await newMember();
    const page = await pageAs(member, '/knowledge');
    await expect(page.getByRole('heading', { name: 'База знаний' })).toBeVisible();

    // Та же сессия завершена вне этой вкладки.
    expect((await member.api.post('/api/auth/logout')).status()).toBe(204);
    await page.getByText('Моя', { exact: true }).click();

    await expect(page).toHaveURL(/\/login$/);
    await expect(page.getByText('Сеанс завершён. Войдите снова')).toBeVisible();

    await fillLogin(page, member.account.login, member.account.password);
    await page.getByRole('textbox', { name: 'Код из приложения' }).fill(await nextCode(member.account));
    await expect(page).toHaveURL(/\/knowledge$/);
  });

  test('сеанс завершён во время ответа: ответ обрывается, интерфейс уходит на вход', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    await send(page, 'Долгий ответ [[stub:slow]]');
    await expect(page.getByRole('button', { name: 'Остановить' })).toBeVisible();

    expect((await member.api.post('/api/auth/logout')).status()).toBe(204);
    await expect(page).toHaveURL(/\/login$/, { timeout: 15_000 });
    await expect(page.getByText('Сеанс завершён. Войдите снова')).toBeVisible();
  });
});

test.describe('Не найдено и нет доступа', () => {
  test('несуществующие чат, запрос, вопрос и разбор — понятные экраны', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, `/chat/${MISSING}`);
    await expect(page.getByRole('heading', { name: 'Чат не найден' })).toBeVisible();
    await expect(page.getByText('Возможно, он удалён')).toBeVisible();

    for (const [path, title] of [
      [`/sql/${MISSING}`, 'Запрос не найден'],
      [`/cogis/${MISSING}`, 'Вопрос не найден'],
      [`/documents/${MISSING}`, 'Разбор не найден'],
    ] as const) {
      await page.goto(path);
      await expect(page.getByRole('heading', { name: title })).toBeVisible();
    }
  });

  test('чужой чат по прямой ссылке выглядит как несуществующий', async ({ newMember, pageAs }) => {
    const owner = await newMember();
    const stranger = await newMember('admin');
    const { dialogId } = await chatWithAnswer(owner.api, `Тайный вопрос ${uniqueSuffix()}`);
    const page = await pageAs(stranger, `/chat/${dialogId}`);
    await expect(page.getByRole('heading', { name: 'Чат не найден' })).toBeVisible();
    await expect(page.locator('main')).not.toContainText('Тайный вопрос');
  });

  test('неизвестный адрес — «Страница не найдена» со ссылкой в чат', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, '/nosuch-page');
    await expect(page.getByRole('heading', { name: 'Страница не найдена' })).toBeVisible();
    await page.getByRole('link', { name: 'Перейти в чат' }).or(page.getByRole('button', { name: 'Перейти в чат' })).click();
    await expect(page).toHaveURL(/\/chat$/);
  });

  test('документ общей базы удалён, пока открыт список: просмотр сообщает об удалении', async ({ newMember, pageAs }) => {
    const author = await newMember();
    const reader = await newMember();
    const mark = `zq${uniqueSuffix()}`;
    const file = textFile(`временный-${mark}.txt`, `Временный документ ${mark} для проверки удаления.`);
    const document = await addKbDocument(author.api, file, { scope: 'shared' });

    const page = await pageAs(reader, '/knowledge');
    // Список с отбором загружен: иначе его запоздалая перезагрузка уберёт строку раньше нажатия.
    const filtered = page.waitForResponse(
      (response) => response.url().includes('/api/kb/documents?') && response.url().includes(`q=${mark}`),
    );
    await page.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(mark);
    await filtered;
    await expect(page.getByRole('button', { name: file.name, exact: true })).toBeVisible();
    expect((await author.api.delete(`/api/kb/documents/${document.id}`)).status()).toBe(204);
    await page.getByRole('button', { name: file.name, exact: true }).click();
    await expect(page.getByText('Документ удалён из базы знаний')).toBeVisible();
  });
});

test.describe('Пустые состояния и проверки ввода', () => {
  test('у нового сотрудника каждый раздел объясняет, с чего начать', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member, '/chat');
    await expect(page.getByRole('heading', { name: 'Чем помочь?' })).toBeVisible();
    await expect(page.getByRole('complementary', { name: 'История' }).getByRole('link')).toHaveCount(0);

    await page.goto('/sql');
    await expect(page.getByRole('heading', { name: 'Опишите задачу — получите запрос' })).toBeVisible();
    await page.goto('/cogis');
    await expect(page.getByRole('heading', { name: 'Плагины CoGIS на C#' })).toBeVisible();
    await page.goto('/documents');
    await expect(page.getByText('Выберите файл или перетащите его сюда').first()).toBeVisible();

    await page.goto('/knowledge');
    await page.getByText('Моя', { exact: true }).click();
    await expect(page.getByRole('heading', { name: 'Здесь будут ваши личные документы' })).toBeVisible();
    await page.getByText('Общая', { exact: true }).click();
    await page.getByRole('textbox', { name: 'Найти по названию или автору' }).fill(`нет-такого-${uniqueSuffix()}`);
    await expect(page.getByRole('heading', { name: 'Ничего не нашлось' })).toBeVisible();
    await expect(page.getByText('Проверьте написание или поищите в другой вкладке')).toBeVisible();
    await page.getByRole('button', { name: 'Сбросить поиск' }).click();
    await expect(page.getByRole('textbox', { name: 'Найти по названию или автору' })).toHaveValue('');
  });

  test('без базы знаний поиск не выполняется: блока источников и заметок о поиске нет', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    const response = await member.api.post(`/api/dialogs/${dialogId}/messages`, {
      data: { content: 'Вопрос без базы', mode: 'fast', knowledge: 'none' },
    });
    await response.text();
    const messages = await (await member.api.get(`/api/dialogs/${dialogId}/messages`)).json();
    // Поиск не выполнялся: источников нет вовсе, а не «ничего не нашлось».
    expect(messages.items[0]).toMatchObject({ sources: null, sources_found: null });
    const page = await pageAs(member, `/chat/${dialogId}`);
    await expectAnswerReady(page);
    await expect(page.getByRole('heading', { name: 'Источники' })).toHaveCount(0);
    await expect(page.getByText('В базе знаний ничего не нашлось')).toHaveCount(0);
  });

  test('документ без текста получает состояние «Ошибка» с причиной', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const name = `пустой-${uniqueSuffix()}.txt`;
    const uploaded = await member.api.post('/api/kb/documents', {
      multipart: { file: textFile(name, ' \n \n'), scope: 'personal', is_cogis: 'false' },
    });
    expect(uploaded.status(), await uploaded.text()).toBe(201);
    const documentId: string = (await uploaded.json()).id;
    await expect
      .poll(async () => (await (await member.api.get(`/api/kb/documents/${documentId}`)).json()).status, {
        timeout: 45_000,
      })
      .toBe('error');

    const page = await pageAs(member, '/knowledge');
    await page.getByText('Моя', { exact: true }).click();
    const row = page.getByRole('row').filter({ hasText: name });
    await expect(row).toContainText('Ошибка');
    await expect(row).toContainText('В документе не нашлось текста.');
    await expectError(await member.api.get(`/api/kb/documents/${documentId}/text`), 409, 'document_not_ready');
  });

  test('слишком длинное сообщение не отправляется', async ({ newMember, pageAs }) => {
    const member = await newMember();
    const page = await pageAs(member);
    // Предел длины сообщения интерфейс берёт из настроек портала.
    await expectChatConfigLoaded(page);
    await composer(page).fill('я'.repeat(32_001));
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
    await expect(page.getByRole('alert')).toHaveText('Сообщение длиннее 32 000 символов. Сократите его');
  });

  test('вложение больше лимита отклоняется сервером с пределом в ответе', async ({ newMember }) => {
    const member = await newMember();
    const dialogId = await createDialog(member.api, 'chat');
    const config = await (await member.api.get('/api/config')).json();
    const limit: number = config.chat.attachment_max_bytes;
    const oversized = Buffer.concat([Buffer.from('%PDF-1.4\n'), Buffer.alloc(limit + 1024, 0x20)]);
    const rejected = await member.api.post(`/api/dialogs/${dialogId}/attachments`, {
      multipart: { file: { name: 'большой.pdf', mimeType: 'application/pdf', buffer: oversized } },
    });
    await expectError(rejected, 413, 'file_too_large');
    expect((await rejected.json()).error.details.max_bytes).toBe(limit);
  });
});
