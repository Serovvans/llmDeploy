# Релиз 0.6: пределы базы знаний — раскатка на работающий 0.5

Пояснения — `runbook.md` §3.4, §3.6, §3.9, §3.10, §4.5, §4.6. Всё — **[ВМ]**: на ВМ не
выполнялось. Команды от `root`, в `tmux`. Команда с ошибкой — стоп, дальше не идти.

```bash
export LIVE=/root/LLM/llmDeploy               # рабочий клон: из него запущен сервис
export REL=/root/LLM/llmRelease               # релизный клон: создаётся на шаге 1
export BAK=/var/lib/docker/portal-backups     # копии: не на диске /
grep -E '^(COMPOSE_PROFILES|GPU_MEM_UTIL)=' "$LIVE/deploy/.env"   # значения для шагов 2 и 6
```

Ниже `PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` — замените на значения из `.env`
(профиль `portal` не указывать). Новых переменных `.env` в 0.6 нет.

## Перед началом

- Работает 0.5: `cd "$LIVE" && make ps && git log -1 --oneline` — все сервисы healthy.
- Владелец слил `feat/kb-limits` в `main` и отправил: `git -C "$LIVE" fetch && git -C "$LIVE" log -1 --oneline origin/main` — коммит релиза 0.6 (далее `<релиз>`). `git -C "$LIVE" status --short` — пусто.
- Место: `df -h /var/lib/docker "$BAK"` — разные устройства; `make release-prepare` сам требует ≥ 135 ГБ.
- Накладка не держит прежние пределы:
   ```bash
   grep -nE 'document_max_bytes|document_max_pages|document_max_chars|min_side_px|max_per_document|description_max_tokens|docx_max_xml_bytes' \
     "$LIVE/deploy/portal/config.override.yaml"
   ```
   Пусто или файла нет — хорошо. Иначе удалить только эти ключи и только с этими
   (прежними) значениями: `kb.document_max_bytes: 52428800`, `kb.document_max_pages: 500`, `kb.indexing.document_max_chars: 3000000`, `kb.images.min_side_px: 100`, `kb.images.max_per_document: 50`, `kb.images.description_max_tokens: 512`, `files.docx_max_xml_bytes: 16777216`. Другие найденные строки (например,
   `kb.rerank.document_max_chars`, `kb.tags.max_per_document`) не трогать (`runbook.md` §3.6).
- `PORTAL_SECRET_KEY` и `BIFROST_ENCRYPTION_KEY` сохранены вне ВМ.

## Подготовка (сервис работает)

1. Релизный клон: `git clone "$(git -C "$LIVE" remote get-url origin)" "$REL" && cd "$REL" && git checkout <релиз>` — `ls "$REL/deploy/.env"` сообщает «нет файла».
2. `cd "$REL" && make release-prepare LIVE="$LIVE" PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` — в конце напечатаны команды окна и отката с тегами; вывод сохранить, записать `<прежний коммит>`.
3. `cd "$REL" && make release-rehearse LIVE="$LIVE"` — строка «репетиция миграций прошла за N с» (применяется 0007).

## Бэкап (начало окна; сообщить сотрудникам о перерыве)

Во время окна `make portal` пересоздаёт и Bifrost: запросы `/v1` ненадолго прервутся —
предупредить и владельцев ключей API.

4. `cd "$REL" && make release-backup LIVE="$LIVE" DIR="$BAK"` — «Копия готова: <путь>», в `ls "$BAK"` нет `.partial`. Путь записать.

## Обновление

5. `cd "$LIVE" && git checkout <релиз>` — `git log -1 --oneline` показывает `<релиз>`.
6. `cd "$LIVE" && make portal PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81` — завершилась без ошибки.
    Что делает: сохраняет прежний `.env` в `deploy/rollback/`, ставит образы портала с
    новым тегом, перезапускает сервисы (миграция `0007` применяется при старте
    `portal-api`), затем пересоздаёт Bifrost и Caddy с новыми `config.json` и `Caddyfile`.

## Проверка

7. Сервисы и шлюз (`config.json` Bifrost в релизе меняется):
    ```bash
    cd "$LIVE" && make ps                         # всё healthy
    make key NAME=smoke REQUESTS=100              # напечатает ключ sk-bf-…
    make smoke KEY=<ключ smoke>
    make smoke-portal
    make revoke ID=<id ключа smoke>
    ```
8. Caddyfile применён — суммы совпадают:
    ```bash
    docker compose -f "$LIVE/deploy/docker-compose.yml" exec caddy sha256sum /etc/caddy/Caddyfile
    sha256sum "$LIVE/deploy/caddy/Caddyfile"
    ```
9. Срок Bifrost 660 с: `curl -s -u "<логин Bifrost>" http://127.0.0.1:8081/api/config | grep -o '"default_request_timeout_in_seconds":[0-9]*'` — 660. Нет в ответе — проверить в веб-интерфейсе Bifrost (провайдер `vllm`) и задать там (`runbook.md` §4.5).
10. В браузере: вход; чат «Быстро»; вопрос по базе знаний с источниками; загрузка PDF 70–200 МБ во вкладке «Моя» — принята (это и есть проверка нового предела Caddy).
11. Журнал воркера без ошибок: `docker compose -f "$LIVE/deploy/docker-compose.yml" logs --no-color --since 2h portal-worker 2>&1 | tail -n 100`. Записи `job yielded` — норма (уступка места, `runbook.md` §3.4).

Всё прошло — сообщить сотрудникам, что окно закончено. Иначе — «Откат».

## Переобработка (после окна, сервис работает)

12. Оценка объёма: `make portal-reprocess-status` — строка «Картинок с описанием прежней версии: N». Время ≈ N × (секунды на описание) / 2 (`runbook.md` §3.10). Картинок после нового извлечения может стать больше N: добавляются VML, порог 40 px, до 300 на документ (`portal-api.md` §11.6).
13. `make portal-reprocess-start IMAGES=1` — затем `make portal-reprocess-status` повторять, пока число ждущих заданий `reprocess` и `context` не станет 0. Оставшиеся в N картинки — у документов не в «Готов» или со сбоем переобработки: они видны в строках сбоев, у каждого понятная причина (`portal-design.md` §10, п. 28). Документы из поиска не выпадают.
14. Чат заметно замедлился — `make portal-reprocess-pause`; продолжить — `make portal-reprocess-resume`.
15. `make portal-reindex ONLY_ERRORS=1` — документы с `document_too_long` (и другими ошибками) снова в очереди; через время в списке они «Готов».
16. Сообщить сотрудникам: файлы, которые раньше не принимались из-за размера или числа страниц, можно загрузить снова (`user-guide.md`, раздел 4).
17. Закрыть релиз, когда откат больше не нужен: `rm -rf "$REL" "$LIVE/deploy/rollback"`; копию шага 4 перенести с ВМ (`runbook.md` §4.4).

## Откат

Нужен, если шаги 6–11 не прошли и выключение флага функции (`runbook.md` §3.8) не помогает.
У миграции `0007` нет обратного хода: после старта нового `portal-api` откат — только с
восстановлением копии шага 4; созданное после неё теряется.

```bash
docker ps -a --filter name=portal-api --format '{{.Image}}'   # тег = 12 символов <релиз> — новый
cd "$LIVE" && git checkout <прежний коммит>
cd "$REL" && make release-rollback LIVE="$LIVE" FROM=<путь копии шага 4>   # ввести yes
cd "$LIVE" && make ps && make smoke-portal                    # healthy, смоук прошёл
```

Если образ `portal-api` прежний (новый не стартовал) — без копии:
`cd "$LIVE" && git checkout <прежний коммит> && cd "$REL" && make release-rollback LIVE="$LIVE"`.
Сбой шага 6 до записи `.env` — `release-rollback` сообщает, что копии `.env` нет:
`cd "$LIVE" && git checkout <прежний коммит> && make up`.
Сбой до шага 5 — ничего не откатывать.

## Что замерить на ВМ

- Секунды на описание одной картинки; укладывается ли в 600 с воркера и 660 с Bifrost.
- Задержка первого токена в чате, пока индексируется длинный скан; частота `job yielded`.
- Память `portal-worker` (`docker stats`) на документе около 10 млн символов.
- Память `portal-api` на DOCX с разметкой около 64 МБ.
- Описания текстовых скриншотов полные, не обрезаны на середине (`portal-design.md` §10, п. 26).
- После шага 13 все отобранные документы переобработаны или имеют понятную причину сбоя в `make portal-reprocess-status` (§10, п. 28).
