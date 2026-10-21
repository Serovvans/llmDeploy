# Релиз 0.7: «Память диалога» — раскатка на работающий 0.6

Пояснения — `runbook.md` §3.4, §3.6, §3.8, §4.6. Всё — **[ВМ]**: на ВМ не выполнялось.
Команды от `root`, в `tmux`. Команда с ошибкой — стоп, дальше не идти. В релизе только
выбор вопросов с ответами и индикатор заполненности; сжатие, перенос и «Продолжить отсюда»
(0.8) не выходят.

```bash
export LIVE=/root/LLM/llmDeploy               # рабочий клон: из него запущен сервис
export REL=/root/LLM/llmRelease               # релизный клон: создаётся на шаге 1
export BAK=/var/lib/docker/portal-backups     # копии: не на диске /
grep -E '^(COMPOSE_PROFILES|GPU_MEM_UTIL)=' "$LIVE/deploy/.env"   # значения для шагов 2 и 6
```

Ниже `PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` — замените на значения из `.env`
(профиль `portal` не указывать).

Новые переменные `.env`: нет. Новый ключ конфигурации: `memory.enabled` (по умолчанию
`true`, `portal/backend/config/app.yaml`). Существующие `llm.chars_per_token` (2.0) и
`llm.tokens_per_image` (3200) после релиза уточняются по журналу (шаги 13–14).
Миграция `0008_dialogs_v07` только добавляет поле `messages.memory_mode` (по умолчанию
`full`): прежние диалоги работают как раньше.

## Перед началом

- Работает 0.6: `cd "$LIVE" && make ps && git log -1 --oneline` — все сервисы healthy.
- Владелец слил `feat/context` в `main` и отправил: `git -C "$LIVE" fetch && git -C "$LIVE" log -1 --oneline origin/main` — коммит релиза 0.7 (далее `<релиз>`). `git -C "$LIVE" status --short` — пусто.
- Место: `df -h /var/lib/docker "$BAK"` — разные устройства.
- `PORTAL_SECRET_KEY` и `BIFROST_ENCRYPTION_KEY` сохранены вне ВМ.
- Риск: если Bifrost v2.2.0 не принимает `stream_options`, которые портал теперь шлёт, чтобы получить фактический размер запроса, ответы в чате перестанут приходить (шаг 9). Первая строка отката — `memory.enabled: false`.

## Подготовка (сервис работает)

1. Релизный клон: `git clone "$(git -C "$LIVE" remote get-url origin)" "$REL" && cd "$REL" && git checkout <релиз>` — `ls "$REL/deploy/.env"` сообщает «нет файла».
2. `cd "$REL" && make release-prepare LIVE="$LIVE" PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` — в конце напечатаны команды окна и отката; вывод сохранить, записать `<прежний коммит>`.
3. `cd "$REL" && make release-rehearse LIVE="$LIVE"` — строка «репетиция миграций прошла за N с» (применяется 0008).

## Бэкап (начало окна; сообщить сотрудникам о перерыве)

4. `cd "$REL" && make release-backup LIVE="$LIVE" DIR="$BAK"` — «Копия готова: <путь>», в `ls "$BAK"` нет `.partial`. Путь записать.

## Обновление

5. `cd "$LIVE" && git checkout <релиз>` — `git log -1 --oneline` показывает `<релиз>`.
6. `cd "$LIVE" && make portal PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` — завершилась без ошибки (миграция `0008` применяется при старте `portal-api`).

## Проверка

7. Сервисы и шлюз:
    ```bash
    cd "$LIVE" && make ps                         # всё healthy
    make key NAME=smoke REQUESTS=100              # напечатает ключ sk-bf-…
    make smoke KEY=<ключ smoke>
    make smoke-portal
    make revoke ID=<id ключа smoke>
    ```
8. Флаг включён: `grep -n -A3 '^memory:' "$LIVE/portal/backend/config/app.yaml"` — `enabled: true`; в `deploy/portal/config.override.yaml` ключа `memory` нет или он `true`.
9. В браузере (сначала старый диалог, затем новый): ответ в чате идёт потоком и завершается — это проверка `stream_options` в Bifrost. Ответ не пришёл — «Откат», первая строка.
10. В заголовке чата кнопка «Память диалога»; под полем ввода индикатор («свободно» в новом чате). Кнопка и индикатор есть также у SQL-помощника, помощника CoGIS и разбора документа.
11. В панели у вопроса выбрать «Не учитывать» — пара в ленте приглушена, подпись «Модель не учитывает»; вернуть «Учитывать».
12. Запись об использовании появилась после ответа:
    ```bash
    docker compose -f "$LIVE/deploy/docker-compose.yml" logs --no-color --since 1h portal-api 2>&1 | grep '"prompt usage"' | tail -n 5
    ```
    В записи поля `answer_id`, `estimated_tokens`, `prompt_tokens`, `text_chars`, `images`; `prompt_tokens` — число, не пусто. Пусто — Bifrost не вернул `usage`: «Откат», первая строка, и сообщить разработчику.

Всё прошло — сообщить сотрудникам, что окно закончено. Иначе — «Откат».

## Калибровка оценки (после окна, в течение рабочих дней)

Оценка заполненности идёт с запасом (`llm.chars_per_token: 2.0`, `llm.tokens_per_image: 3200`);
по фактическим числам её уточняют. Нужны пары «оценка — факт»: 20–50 записей разных диалогов,
в том числе с картинками и длинные.

13. Собрать записи (только числа, без текста сообщений):
    ```bash
    docker compose -f "$LIVE/deploy/docker-compose.yml" logs --no-color --since 72h portal-api 2>&1 | grep '"prompt usage"' > "$BAK/prompt-usage.log"
    wc -l "$BAK/prompt-usage.log"
    ```
14. Посчитать:
    - записи без картинок (`images` = 0): `chars_per_token` ≈ `text_chars` / `prompt_tokens` (по медиане и по худшему случаю — наименьшему отношению);
    - записи с картинками: `tokens_per_image` ≈ (`prompt_tokens` − `text_chars` / `chars_per_token`) / `images`.
    Сравнить `estimated_tokens` с `prompt_tokens`: оценка должна быть не ниже факта; заниженная оценка — опасна (диалог обрежется без предупреждения), завышенная в пределах 20–30 % — норма.
15. Записать значения в накладку `deploy/portal/config.override.yaml` (`runbook.md` §3.6) и перезапустить:
    ```yaml
    llm:
      chars_per_token: <значение>
      tokens_per_image: <значение>
    ```
    ```bash
    docker compose -f "$LIVE/deploy/docker-compose.yml" restart portal-api portal-worker
    make ps    # healthy
    ```
    Проверка: новые записи `prompt usage` — `estimated_tokens` не ниже `prompt_tokens`.
16. Результат (числа и вывод) передать разработчику: релиз 0.8 выходит только после калибровки (`portal-design.md` §8.4).
17. Закрыть релиз, когда откат больше не нужен: `rm -rf "$REL" "$LIVE/deploy/rollback"`; копию шага 4 перенести с ВМ (`runbook.md` §4.4).

## Откат

1. Первая линия — флаг, без отката кода и данных. В `deploy/portal/config.override.yaml`:
    ```yaml
    memory:
      enabled: false
    ```
    ```bash
    docker compose -f "$LIVE/deploy/docker-compose.yml" restart portal-api portal-worker
    ```
    Проверка: в браузере нет кнопки «Память диалога» и индикатора; ответ в чате идёт. Запрос к модели собирается как в 0.6 и `stream_options` не шлёт; уже сделанный выбор «Не учитывать» и «Без вложений» продолжает действовать.
2. Откат кода. У миграции `0008` нет обратного хода: после старта нового `portal-api` код откатывается только с восстановлением копии шага 4 (`portal-design.md` §8.4); созданное после неё теряется.
    ```bash
    docker ps -a --filter name=portal-api --format '{{.Image}}'   # тег = 12 символов <релиз> — новый
    cd "$LIVE" && git checkout <прежний коммит>
    cd "$REL" && make release-rollback LIVE="$LIVE" FROM=<путь копии шага 4>   # ввести yes
    cd "$LIVE" && make ps && make smoke-portal                    # healthy, смоук прошёл
    ```
    Если образ `portal-api` прежний (новый не стартовал) — без копии:
    `cd "$LIVE" && git checkout <прежний коммит> && cd "$REL" && make release-rollback LIVE="$LIVE"`.
    Сбой шага 6 до записи `.env`: `cd "$LIVE" && git checkout <прежний коммит> && make up`.
    Сбой до шага 5 — ничего не откатывать.

## Что замерить на ВМ

- Точность оценки: отношение `estimated_tokens` / `prompt_tokens` на 20–50 ответах (шаги 13–14).
- Приходит ли `usage` от Bifrost на каждый ответ (шаг 12).
- Индикатор и панель в светлой и тёмной темах при ширине окна 1280 и 1920 px.
