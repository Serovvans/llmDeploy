# Релиз 0.5: раскатка на работающую ВМ

Линейная последовательность команд. Пояснения к шагам и разбор сбоев — `runbook.md` §4.6,
прерванное восстановление — §4.4.

Правила:

- Все команды выполняются на ВМ от `root`, если не сказано иное.
- Шаги 2–15 выполняйте в `tmux`: обрыв SSH не должен прервать команду.
- Если команда завершилась ошибкой — остановитесь. Следующую команду не выполняйте, пока
  причина не устранена.
- Блоки команд выполняйте по одному, проверяя условие после блока.

Задайте переменные в каждой новой сессии терминала:

```bash
export LIVE=/root/LLM/llmDeploy               # рабочий клон: из него запущен сервис
export REL=/root/LLM/llmRelease               # релизный клон: создаётся на шаге 4
export BAK=/var/lib/docker/portal-backups     # резервные копии: диск 1 ТБ
```

Простой сервиса: шаг 2 (перенос и перезагрузка), шаг 8 (копии и восстановление),
шаги 10–13 (окно релиза).

## Часть 1. До окна

### Шаг 1. Слить ветку и записать коммиты

На рабочей станции:

```bash
git status --short                            # вывод пуст
git checkout main && git merge --no-ff feat/portal && git push origin main
git rev-parse HEAD                            # релизный коммит — записать
```

На ВМ:

```bash
git -C "$LIVE" rev-parse HEAD                 # прежний коммит — записать
grep '^COMPOSE_PROFILES=' "$LIVE/deploy/.env"
```

Если в `COMPOSE_PROFILES` есть `monitoring`, во всех командах ниже вместо
`PROFILES=embeddings,rerank` пишите `PROFILES=embeddings,monitoring,rerank`.

### Шаг 2. Перенести хранилище образов на диск 1 ТБ (простой)

Образы Docker лежат в `/var/lib/containerd` на диске `/`, где свободно 8,7 ГБ: образы
релиза туда не поместятся. Данные сервиса шаг не затрагивает: тома лежат в
`/var/lib/docker/volumes`. Нужен доступ к консоли ВМ на случай сбоя перезагрузки.

2.1. Проверить условия. Первая команда должна напечатать строку с `/var/lib/docker`; если
вывод пуст — остановиться: диск 1 ТБ не записан в `/etc/fstab`.

```bash
findmnt --fstab /var/lib/docker
test ! -e /var/lib/containerd.old && test ! -e /var/lib/docker/containerd-root && echo МОЖНО
docker images --digests | sort > /root/images-before.txt
cp -a /etc/fstab /etc/fstab.before-release-0.5
```

2.2. Остановить сервис и Docker. Последняя команда не должна ничего напечатать; если
напечатала — не продолжать, выполнить `systemctl start containerd docker` и повторить
блок.

```bash
cd "$LIVE" && make down
systemctl stop docker.socket docker containerd
mount | grep /var/lib/containerd
```

2.3. Скопировать. Продолжать только после строки `КОПИЯ ГОТОВА`.

```bash
mkdir /var/lib/docker/containerd-root
rsync -aHAXS --numeric-ids /var/lib/containerd/ /var/lib/docker/containerd-root/ && echo КОПИЯ ГОТОВА
```

2.4. Подключить новое хранилище по прежнему пути. `findmnt --verify` не должен сообщить
об ошибках.

```bash
mv -T /var/lib/containerd /var/lib/containerd.old && mkdir /var/lib/containerd
echo '/var/lib/docker/containerd-root /var/lib/containerd none bind,x-systemd.requires-mounts-for=/var/lib/docker 0 0' >> /etc/fstab
systemctl daemon-reload && mount /var/lib/containerd
findmnt --verify
systemctl start containerd docker
```

2.5. Проверить. `diff` не должен ничего напечатать; `df` должен показать устройство
`nvme1n1p1`.

```bash
df -h /var/lib/containerd
docker images --digests | sort | diff - /root/images-before.txt
cd "$LIVE" && make up && make ps && make smoke-portal
```

2.6. Перезагрузить ВМ и проверить, что хранилище подключается само.

```bash
reboot
```

```bash
df -h /var/lib/containerd /var/lib/docker     # оба — nvme1n1p1
systemctl is-active llm-stack
cd "$LIVE" && make ps && make smoke-portal
```

Если ВМ после перезагрузки недоступна — в консоли проверить монтирование
`/var/lib/docker` и `/var/lib/containerd`.

Каталог `/var/lib/containerd.old` не удалять до шага 16.

**Возврат шага 2.** Возможен только до шага 5. Если проверка 2.5 или 2.6 не прошла:

```bash
cd "$LIVE" && docker compose -f deploy/docker-compose.yml down
systemctl stop docker.socket docker containerd
if mountpoint -q /var/lib/containerd; then umount /var/lib/containerd; fi
rmdir /var/lib/containerd && mv -T /var/lib/containerd.old /var/lib/containerd
cp -a /etc/fstab.before-release-0.5 /etc/fstab
systemctl daemon-reload && systemctl start containerd docker
cd "$LIVE" && make up
```

### Шаг 3. Создать каталог резервных копий

```bash
mkdir -p -m 700 "$BAK"
df -h / "$BAK"                                # разные устройства в первом столбце
```

Копия лежит на одном диске с данными: она защищает от неудачного релиза, но не от потери
диска.

### Шаг 4. Создать релизный клон

```bash
git clone "$(git -C "$LIVE" remote get-url origin)" "$REL"
cd "$REL" && git checkout <релизный коммит>
```

Проверить рабочий клон: вторая команда не должна сообщить об ошибке, третья — ничего
напечатать. Рабочий каталог при этом не меняется.

```bash
git -C "$LIVE" fetch
git -C "$LIVE" cat-file -e <релизный коммит>
git -C "$LIVE" status --short
```

Файла `deploy/.env` в релизном клоне быть не должно. Клон не удалять и не переключать до
шага 16: из него выполняется откат.

### Шаг 5. Подготовить релиз

Собирает образы, загружает веса, помечает нынешние образы портала тегом отката.
Работающий сервис и рабочий клон не меняются.

```bash
cd "$REL" && make release-prepare LIVE="$LIVE" PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81
docker image ls 'llm-*'                       # образы портала под двумя тегами
df -h /                                       # свободного места не убавилось
```

Вывод команды сохранить: в конце она печатает команды окна и отката с тегами. Тег
образа — первые 12 символов коммита. До шага 16 не выполнять `docker image prune`.

### Шаг 6. Проверить миграции на копии базы

```bash
cd "$REL" && make release-rehearse LIVE="$LIVE"
```

Успех — строка «репетиция миграций прошла за N с». При ошибке релиз не выкатывать.

### Шаг 7. Сохранить секреты вне ВМ

Скопировать из `$LIVE/deploy/.env` в хранилище секретов значения `PORTAL_SECRET_KEY` и
`BIFROST_ENCRYPTION_KEY`. Без них резервная копия не восстанавливается.

### Шаг 8. Проверить восстановление из копии (простой)

Откат релиза опирается на восстановление из копии, а на ВМ оно ни разу не выполнялось.
Восстановление удаляет действующие данные и заменяет их копией, поэтому сначала снимается
вторая, независимая копия томов. Предупредите сотрудников: созданное во время шага
теряется.

8.1. Независимая копия томов при остановленном сервисе. Продолжать только после строки
`ТОМА СКОПИРОВАНЫ`.

```bash
cd "$LIVE" && make down
mkdir -m 700 "$BAK/raw"
cp -a /var/lib/docker/volumes/llm_portal-db-data /var/lib/docker/volumes/llm_portal-files \
      /var/lib/docker/volumes/llm_qdrant-data /var/lib/docker/volumes/llm_bifrost-data "$BAK/raw/" && echo ТОМА СКОПИРОВАНЫ
make up
```

8.2. Резервная копия. Продолжать только после строки «Копия готова: <путь>»; путь
записать.

```bash
cd "$REL" && make release-backup LIVE="$LIVE" DIR="$BAK"
```

8.3. Восстановление. Команда проверит копию и спросит подтверждение — сразу ввести `yes`.

```bash
cd "$LIVE" && time make restore FROM=<путь копии из 8.2>
make ps && make smoke-portal
```

Войти на портал и убедиться, что чаты, документы и ключи API на месте. Записать
длительность копии и восстановления: это время отката в окне.

Если восстановление прервано — повторить ту же команду `make restore`, затем `make up`.

Если после восстановления данных не хватает — вернуть тома из независимой копии:

```bash
cd "$LIVE" && make down
for v in llm_portal-db-data llm_portal-files llm_qdrant-data llm_bifrost-data; do
  rm -rf "/var/lib/docker/volumes/$v" && cp -a "$BAK/raw/$v" /var/lib/docker/volumes/ || break
done
make up
```

В этом случае релиз не выкатывать.

8.4. После успешной проверки удалить обе копии шага: в окне в каталоге должна остаться
одна копия.

```bash
rm -rf "$BAK/raw" <путь копии из 8.2>
ls -A "$BAK"                                  # вывод пуст
```

### Шаг 9. Объявить окно

Сообщить сотрудникам и владельцам ключей API время окна: портал и `/v1` работают с
перерывами; созданное в окне при откате теряется.

## Часть 2. Окно

### Шаг 10. Снять резервную копию и переключить рабочий клон

```bash
cd "$REL" && make release-backup LIVE="$LIVE" DIR="$BAK"
ls "$BAK"
```

Продолжать, только если команда напечатала «Копия готова: <путь>» и в каталоге нет имён
с окончанием `.partial`. Иначе повторить блок; рабочий клон не переключать. Путь копии
записать: он нужен для отката.

```bash
cd "$LIVE" && git checkout <релизный коммит>
```

С этого момента и до успешного завершения шага 11 или отката не выполнять `make up` и не
перезагружать ВМ.

### Шаг 11. Установить релиз

```bash
cd "$LIVE" && make portal PROFILES=embeddings,rerank GPU_MEM_UTIL=0.81
```

Команда перезапускает модели по очереди, затем сервисы портала (миграции применяются при
старте `portal-api`), затем Bifrost и Caddy. При ошибке — раздел «Откат».

### Шаг 12. Выдать ключу портала доступ к реранкеру

```bash
cd "$LIVE"
make keys                                     # найти id ключа portal
make key-rerank ID=<id ключа portal>
```

### Шаг 13. Проверить

```bash
cd "$LIVE"
make key NAME=smoke REQUESTS=100              # напечатает ключ sk-bf-…
make smoke KEY=<ключ smoke>
make smoke-portal
make revoke ID=<id ключа smoke>
nvidia-smi                                    # три процесса vLLM
docker compose -f "$LIVE/deploy/docker-compose.yml" logs vllm | grep 'Maximum concurrency'   # не меньше 8
df -h / /var/lib/docker
```

В браузере:

1. Вход на портал.
2. Чат в режимах «Быстро» и «Вдумчиво» — оба отвечают.
3. Вопрос с базой знаний — ответ с источниками.
4. Загрузка документа с тегом.
5. Загрузка аудиозаписи больше 64 МБ.

Журнал Bifrost не должен содержать текста запросов, включая запросы реранкера: проверка —
`vpn-launch.md`, п. 45. Если текст есть — выключить `kb.rerank.enabled` (`runbook.md`
§3.8) и очистить журнал (§4.5).

Все проверки прошли — сообщить сотрудникам, что окно закончено. Проверка не прошла —
раздел «Откат».

## Часть 3. После окна

### Шаг 14. Переобработать документы

Документы из поиска не выпадают: прежний индекс работает, пока не готов новый.

```bash
cd "$LIVE" && make portal-eval-search
```

По результату замера решить, остаются ли включёнными `kb.context` и `kb.images`
(`runbook.md` §3.8). Решение принимается до переобработки: после смены этих флагов её
придётся повторять.

```bash
make portal-reprocess-start
make portal-reprocess-status                  # повторять до пустой очереди
```

Если чат заметно замедлился — `make portal-reprocess-pause`, продолжить —
`make portal-reprocess-resume`. Переобработка закончена, когда в очереди нет заданий
переобработки и пояснений.

### Шаг 15. Включить проверку SQL

До включения — проверка изоляции песочницы: `vpn-launch.md`, п. 59. Если она не прошла,
флаг не включать.

```bash
cat >> "$LIVE/deploy/portal/config.override.yaml" <<'EOF'
sql:
  sandbox:
    enabled: true
EOF
cd "$LIVE" && docker compose -f deploy/docker-compose.yml restart portal-api portal-worker
```

Если в файле уже есть раздел `sql`, добавьте в него `sandbox.enabled: true` вручную, не
дублируя раздел.

После включения — проверки `vpn-launch.md`, п. 60–61. Если тестовые данные модели не
покрывают условия запросов — вернуть `enabled: false` и повторить перезапуск.

### Шаг 16. Закрыть релиз

Выполняется, когда переобработка завершена и откат больше не рассматривается.

```bash
findmnt /var/lib/containerd                   # источник — /var/lib/docker/containerd-root
rm -rf /var/lib/containerd.old                # вернёт 33 ГБ на диске /
docker ps -a --filter name=llm-rehearsal-db   # вывод пуст
rm -rf "$REL" "$LIVE/deploy/rollback"         # в rollback — секреты прежнего .env
rm /root/images-before.txt /etc/fstab.before-release-0.5
```

Резервную копию шага 10 перенести на другую машину и хранить как секрет (`runbook.md`
§4.4); с ВМ удалить.

## Откат

Определить состояние:

```bash
ls -A "$LIVE/deploy/rollback"                 # каталога нет — .env не менялся
docker ps -a --filter name=portal-api --format '{{.Image}}  {{.Status}}'
```

Образ `llm-portal-api` без тега или с тегом прежнего коммита — прежний. Образ
`llm-portal-api:<первые 12 символов релизного коммита>` — новый.

| Состояние | Действие | Данные |
|---|---|---|
| Сбой одной функции после релиза | Выключить её флаг (`runbook.md` §3.8); релиз не откатывать | Не затронуты |
| Сбой на шаге 2 | Блок «Возврат шага 2» | Не затронуты |
| Сбой на шаге 8 | Указания шага 8 | По шагу 8 |
| Сбой на шагах 3–7 или при снятии копии на шаге 10 | Ничего; если сервисы остановлены — `cd "$LIVE" && make up` | Не затронуты |
| После `git checkout` шага 10, каталога `deploy/rollback` нет | `cd "$LIVE" && git checkout <прежний коммит> && make up` | Не затронуты |
| Каталог `deploy/rollback` есть, образ `portal-api` прежний | Устранить причину и повторить шаг 11 либо откат без копии | Не затронуты |
| Образ `portal-api` новый, либо состояние определить не удалось | Откат с копией | Созданное после шага 10 теряется |

Вернуть рабочий клон на прежний коммит:

```bash
cd "$LIVE" && git checkout <прежний коммит>
```

Затем выполнить **один** из двух вариантов по таблице.

Откат без копии:

```bash
cd "$REL" && make release-rollback LIVE="$LIVE"
```

Откат с копией. Команда проверит копию и спросит подтверждение — ввести `yes`:

```bash
ls -d "$BAK"/llm-backup-*                     # одна копия — снятая на шаге 10
cd "$REL" && make release-rollback LIVE="$LIVE" FROM=<путь копии шага 10>
```

Проверка после отката:

```bash
cd "$LIVE" && make ps && make smoke-portal
```
