# Runbook: установка и эксплуатация LLM-сервиса

Практическая инструкция для администратора ВМ. Решения и их обоснование — в
[`design.md`](design.md), сетевые требования — в [`devops-request.md`](devops-request.md).

Пометка **[ВМ]** — шаг не проверялся локально (нет GPU/Docker), его результат нужно
подтвердить при первой установке и при необходимости поправить этот документ.

## 0. Как устроен проект

```
Клиент ──HTTPS:443──▶ Caddy ──/v1/*──▶ Bifrost (ключи sk-bf-, лимиты) ──▶ vLLM (модель "default")
                                          127.0.0.1:8080 (админка)        [vllm-embed "embeddings"]
```

| Каталог | Что там |
|---|---|
| `deploy/` | `docker-compose.yml`, `.env.example`, `caddy/`, `bifrost/config.json`, `monitoring/`, `systemd/` |
| `Makefile` | однострочные команды: `make help` |
| `scripts/` | `install_host.sh`, `install.sh`, `preflight.sh`, `smoke_test.py`, `keys.py`, `bench.sh`, `with_env.sh`, тесты (uv-проект) |
| `docs/` | дизайн, заявка DevOps, этот runbook |
| `.claude/agents/` | агенты для разработки: `infra-engineer`, `scripts-engineer`, `reviewer` |

Compose-профили: базовый (только vllm), `gateway` (caddy, bifrost — второй этап
развёртывания, `design.md` §3.7), `embeddings` (vllm-embed), `monitoring` (dcgm-exporter,
Prometheus, Grafana). Имя проекта фиксировано — `llm`, поэтому volumes называются
`llm_hf-cache`, `llm_bifrost-data`, `llm_caddy-data`, `llm_prometheus-data`.

## 1. Перед установкой

Модель (этап 1) разворачивается сразу после получения ВМ: из списка ниже нужны только
сама ВМ и исходящий доступ. DNS-имя, сертификат и входящий 443 нужны на этапе 2 и не
блокируют первый.

От DevOps (см. `devops-request.md`) нужно получить:
- ВМ: Ubuntu 24.04 или 22.04, ≥ 8 vCPU, ≥ 64 ГБ RAM, ≥ 300 ГБ SSD, GPU проброшен целиком,
  доступ по SSH с sudo;
- DNS-имя `llm.<корп.домен>`, которое резолвится у VPN-клиентов и на серверах приложений;
- правила файрвола: входящие 443 (клиенты) и 22 (админы); исходящий доступ к Hugging
  Face, реестрам образов и apt-репозиториям Docker и NVIDIA;
- сертификат корпоративного CA на это имя, если его можно выпустить (см. 1.1).

Драйвер, Docker, NVIDIA Container Toolkit и uv ставит `install_host.sh` (2.1), заранее
их устанавливать не нужно.

### 1.1 TLS: корпоративный сертификат или внутренний CA Caddy

| | `TLS_MODE=corp` (основной вариант) | `TLS_MODE=internal` (сертификата нет) |
|---|---|---|
| Что нужно | `fullchain.pem` + `privkey.pem` от корпоративного CA | Ничего: Caddy сам создаёт CA и выпускает сертификат |
| Доверие клиентов | Уже есть: корпоративный CA распространён через GPO | Корневой `deploy/caddy-root.crt` раздаётся серверам приложений и UI (3.2) |
| Продление | Вручную, по сроку сертификата (4.3) | Автоматически; корень действует 10 лет |
| Что бэкапить | Ничего сверх `bifrost-data` | Ещё volume `llm_caddy-data`, в нём ключ корня (4.4) |

**Если корпоративного сертификата нет**, ставьте с `TLS_MODE=internal`: сервис заработает
сразу, без интернета и без DevOps. Клиентов немного (серверы приложений и UI, сотрудники
ходят через UI), поэтому раздать им один файл `caddy-root.crt` несложно. Доверяйте этому
корню **на уровне приложения**, а не в системном хранилище клиента: ключ корня лежит на
ВМ, и системное доверие распространилось бы на любые домены. Когда корпоративный
сертификат появится, перейдите на `corp` (4.3), клиентский код менять не придётся.

Почему не другие варианты: Let's Encrypt требует публичной DNS-зоны с API и сборки Caddy
с плагином, к тому же имя хоста попадёт в публичные CT-логи. Самоподписанный сертификат
не продлевается сам, и клиенты в итоге отключают проверку. HTTP без TLS передаёт ключи
`sk-bf-` открытым текстом (`design.md` §3.4).

## 2. Установка

Установка разбита на два этапа (`design.md` §3.7). Первый не зависит от DevOps: модель
разворачивается и проверяется сразу. Второй добавляет внешний доступ, когда появятся
DNS-имя, сертификат и правило файрвола на 443.

Все команды выполняются на ВМ, из каталога с клоном репозитория. `make` сам подставляет
`sudo`, если запущен не от root; `make help` показывает список целей.

```bash
sudo apt-get install -y git make
sudo git clone <репозиторий> /opt/llm && cd /opt/llm

# --- этап 1: модель ---
make host                 # 2.1; если попросит — sudo reboot и снова cd /opt/llm
make preflight            # 2.2; дальше только при «ИТОГ: PASS»
make model                # 2.3; можно с PROFILES=monitoring
make smoke-model          # 2.4
make bench                # 2.4

# --- этап 2: внешний доступ ---
# 2.5, только для TLS_MODE=corp: положить deploy/certs/fullchain.pem и privkey.pem
make gateway LLM_HOSTNAME=llm.<корп.домен> TLS_MODE=corp        # 2.6
# или без корпоративного сертификата:
make gateway LLM_HOSTNAME=llm.<корп.домен> TLS_MODE=internal
make key NAME=smoke && make smoke KEY=sk-bf-...                 # 2.7
```

Путь `/opt/llm` не обязателен: `install.sh` пропишет в systemd-юнит фактический путь к
клону. Без `make` те же шаги делаются вызовом `scripts/*.sh` напрямую — что именно
запускает каждая цель, видно в `Makefile`.

### 2.1 Подготовка ВМ: `install_host.sh`

```bash
make host        # = sudo bash scripts/install_host.sh
```

Скрипт ставит:
- драйвер `nvidia-driver-580-server-open`;
- Docker Engine и compose-плагин из `download.docker.com`, проверяет, что версия ≥ 27;
- NVIDIA Container Toolkit из `nvidia.github.io` и регистрирует runtime `nvidia` в Docker;
- `uv` закреплённой версии в `/usr/local/bin`;
- синхронизацию времени по NTP.

Скрипт идемпотентен: установленные компоненты пропускаются, запускать повторно
безопасно. Если в системе уже есть драйвер NVIDIA другой ветки или пакет `docker.io`,
скрипт ничего не удаляет: он останавливается и выводит команду, которую нужно выполнить.
После установки драйвера скрипт напишет «НУЖНА ПЕРЕЗАГРУЗКА»: выполните `sudo reboot`.

### 2.2 Проверка готовности: `preflight.sh`

```bash
make preflight   # = sudo bash scripts/preflight.sh
```

Что проверяется:
- GPU и объём VRAM;
- драйвер ≥ 580 с open kernel modules;
- Docker ≥ 27 и запуск контейнера с `--gpus all`;
- место на диске, RAM, vCPU;
- доступ к Hugging Face: реальная загрузка `config.json` и первого КиБ весов через Xet CDN;
- доступ к реестрам образов, NTP, наличие `uv`.

Для каждой проваленной проверки скрипт выводит, что делать. Если есть провалы, код
выхода 1. `make model` сам запускает preflight первым шагом, отдельный запуск нужен,
чтобы исправить окружение до установки.

### 2.3 Этап 1: развёртывание модели

```bash
make model                        # = sudo bash scripts/install.sh --stage model
make model PROFILES=monitoring    # вместе с Prometheus и Grafana
```

Поднимается только vLLM: DNS-имя, сертификат и открытый 443 на этом этапе не нужны.
Модель доступна на `127.0.0.1:8000` (наружу порт не выставляется) и требует внутренний
ключ `VLLM_API_KEY`.

| Параметр | Значение |
|---|---|
| `PROFILES` | пусто (по умолчанию), `monitoring`, `embeddings`, `embeddings,monitoring` |

Что делает скрипт, по шагам:
1. Запускает `preflight.sh`.
2. Создаёт `deploy/.env` (root, 600) и генерирует пустые секреты: `VLLM_API_KEY`,
   `BIFROST_ADMIN_PASSWORD`, `BIFROST_ENCRYPTION_KEY`, `GRAFANA_ADMIN_PASSWORD`. Логин
   админки Bifrost — `admin`. Уже заданные значения не меняются. Секреты Bifrost
   генерируются сразу, хотя понадобятся только на втором этапе.
3. Выполняет `docker compose config` и `pull`.
4. Скачивает веса моделей в volume `llm_hf-cache`. Это ~30 ГБ, занимает десятки минут.
   Отдельный шаг нужен, потому что загрузка внутри запуска могла бы не уложиться в
   `start_period` healthcheck vLLM.
5. Устанавливает и включает systemd-юнит `llm-stack`, запускает стек и ждёт, пока все
   сервисы станут healthy. Пока vLLM загружает модель, логи смотрите в другом терминале:
   `make logs SERVICE=vllm`.
6. Проверяет на `127.0.0.1:8000`, что запрос без ключа отклоняется (401), а с ключом
   модель отвечает.

Повторный запуск применяет изменения. Параметры модели (`MODEL_ID`, `MAX_MODEL_LEN`,
`GPU_MEM_UTIL`, `HF_HUB_OFFLINE`, описание — `design.md` §6.1) правятся в `deploy/.env`,
после чего выполняется `sudo bash scripts/install.sh --skip-preflight`. Если используются
эмбеддинги, `GPU_MEM_UTIL` + 0.06 не должно превышать 0.92.

Критерий приёмки **[ВМ]**: в логе vLLM есть строка
`Maximum concurrency for 65536 tokens per request: N`, и **N ≥ 8**. Если меньше,
добавьте `--kv-cache-dtype fp8` (`design.md` §5).

### 2.4 Проверка модели и бенчмарк

```bash
make smoke-model                  # smoke_test.py --direct на 127.0.0.1:8000
make bench                        # 8 параллельных запросов; CONCURRENCY=16 — другая параллельность
```

`make smoke-model` проверяет саму модель, без TLS и gateway:
- генерацию текста;
- распознавание изображения;
- `json_schema`;
- tool call;
- отключение мышления через `chat_template_kwargs`;
- 401 без ключа и с неверным ключом.

Проверки, относящиеся к обвязке (404 на `/api/*`, `/metrics` и `/`), выполняются на
втором этапе (2.7). Если что-то не прошло, см. раздел 5.

Бенчмарк гоняет `vllm bench serve` внутри контейнера напрямую против vLLM: измеряется
сама модель. Результаты сохраняются в `scripts/bench-results/`. Зафиксируйте TTFT и
throughput после первого этапа, их берут за базу при обновлениях.

С рабочей станции модель доступна через туннель:

```bash
ssh -L 8000:127.0.0.1:8000 <админ>@<вм>
```

Отдельные проверки при первой установке **[ВМ]**:
- `reasoning_effort` и `chat_template_kwargs` доходят до vLLM;
- vllm-embed стартует при util 0.06 (профиль `embeddings`);
- после `sudo reboot` стек поднимается сам: `make ps`.

### 2.5 Сертификат (только `TLS_MODE=corp`)

```bash
sudo install -d -m 700 /opt/llm/deploy/certs
sudo install -m 644 fullchain.pem /opt/llm/deploy/certs/fullchain.pem
sudo install -m 600 privkey.pem  /opt/llm/deploy/certs/privkey.pem
```

В `fullchain.pem` должна быть вся цепочка, включая промежуточный CA. Каталог `certs/`
в `.gitignore`. `make gateway` проверит, что сертификат не просрочен, выписан на
`LLM_HOSTNAME` и соответствует ключу. Если до истечения меньше 30 дней, выведет
предупреждение.

### 2.6 Этап 2: внешний доступ

```bash
make gateway LLM_HOSTNAME=llm.<корп.домен> TLS_MODE=corp
```

| Параметр | Значение |
|---|---|
| `LLM_HOSTNAME` | DNS-имя сервиса |
| `TLS_MODE` | `corp` или `internal` (1.1) |
| `PROFILES` | если нужно поменять набор дополнительных профилей |

Команда добавляет к работающей модели профиль `gateway` — Caddy и Bifrost — и:
1. Проверяет сертификат для `corp` (2.5).
2. Выполняет `docker compose pull` и поднимает стек целиком, ожидая healthy.
3. Для `internal` выгружает корневой сертификат в `deploy/caddy-root.crt`.
4. Проверяет через 443, что запрос к модели без ключа получает 401/403, а `/` — 404.

Заданные значения записываются в `deploy/.env` (`LLM_HOSTNAME`, `TLS_MODE`,
`COMPOSE_PROFILES`), поэтому повторный `make gateway` можно запускать без параметров.
Профиль `gateway` из `COMPOSE_PROFILES` сам не убирается: чтобы вернуться к одной
модели, уберите его из `deploy/.env` и выполните `make up`.

Preflight на этом этапе не повторяется: готовность ВМ уже подтверждена на первом.

> **Сразу после установки** сохраните копию `BIFROST_ENCRYPTION_KEY` из `deploy/.env`
> вне ВМ, в хранилище секретов заказчика. Без неё базу Bifrost (ключи, лимиты) из
> бэкапа не восстановить, а сменить ключ можно только миграцией.

### 2.7 Проверка сервиса через HTTPS

```bash
# 1. Ключ для смоук-теста и ключ с маленьким лимитом для проверки 429
make key NAME=smoke REQUESTS=100
make key NAME=smoke-limit REQUESTS=3

# 2. Основной смоук-тест через HTTPS. При TLS_MODE=internal корневой сертификат
#    подставляется сам; для corp укажите CA=<PEM корпоративного корневого CA>
make smoke KEY=sk-bf-...

# 3. Проверка лимита
make smoke KEY=<ключ smoke-limit> ARGS="--check-rate-limit 5"

# 4. Ключи сохраняются после перезапуска
sudo docker compose -f deploy/docker-compose.yml restart bifrost
make keys
```

`LLM_CA_CERT` обязателен для `corp`: httpx не читает системное хранилище сертификатов.

Смоук-тест выполняет те же проверки модели, что и `make smoke-model`, плюс проверки
обвязки: 401/403 без ключа и с неверным ключом, 404 на `/api/*`, `/metrics` и `/`
через 443. Если какая-то проверка не прошла, сверьтесь с разделом 5. Затем отзовите
тестовые ключи: `make revoke ID=<id>`.

Отдельные проверки при первой установке **[ВМ]**:
- `/v1/*` с ключом `sk-bf-...` работает при включённой admin-auth. По исходникам v2.2.0
  это так; документация Bifrost намекает на `disable_auth_on_inference`. Если не
  работает, добавить этот параметр в `config.json`, предварительно внеся в дизайн;
- модель `default` принимается Bifrost без префикса `vllm/`;
- `curl -s http://127.0.0.1:8080/metrics` отвечает без пароля, а
  `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/governance/virtual-keys`
  без Basic-auth возвращает 401.

С рабочей станции администратора UI Bifrost и Grafana открываются через туннель:

```bash
ssh -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 <админ>@<вм>
```

### 2.8 Предзагрузка весов вручную

Используется при смене модели (4.1). То же самое делает шаг 4 `install.sh` **[ВМ]**:

```bash
make preload
```

После загрузки можно поставить `HF_HUB_OFFLINE=1`: сервис перестанет обращаться к
Hugging Face.

## 3. Ежедневная эксплуатация

### 3.1 API-ключи

Ключи выпускаются по одному на приложение и по одному на сотрудника (или на
UI-сервер). Команды выполняются на ВМ, из каталога с клоном; логин и пароль admin-auth
Bifrost подставляются из `deploy/.env`.

```bash
make key NAME=app-crm REQUESTS=600 TOKENS=500000   # выводит id и sk-bf-...
make keys                                          # расход/лимиты, значения ключей не показываются
make revoke ID=<id>                                # деактивация: доступ пропадает сразу
```

Описание и период задаются полными аргументами `keys.py`:
`sudo bash scripts/with_env.sh uv run keys.py create --name app-crm --description "CRM" --period 1h`.

Значение `sk-bf-...` показывается один раз, при создании. Передавайте его владельцу
по защищённому каналу. То же можно сделать в UI Bifrost: `http://localhost:8080`
через туннель (2.7).

### 3.2 Что сообщить клиентам

```python
from openai import OpenAI
client = OpenAI(base_url="https://llm.<корп.домен>/v1", api_key="sk-bf-...")
client.chat.completions.create(
    model="default",
    messages=[{"role": "user", "content": "..."}],
    reasoning_effort="low",   # для извлечения/классификации; по умолчанию xhigh — медленно
)
```

- Модель всегда `default`: при смене модели на сервере код клиентов не меняется.
- Допустимые значения `reasoning_effort`: `low`, `medium`, `xhigh`. Значение
  **`high` даёт HTTP 500**.
- Мышление отключается полностью через
  `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`.
- Изображения передаются как `image_url` (data URL или ссылка), до 8 штук на запрос.
  Контекст — до 64k токенов.
- Эмбеддинги доступны только при включённом профиле: `model="embeddings"`,
  `/v1/embeddings`.
- Клиенту нужен корневой сертификат: при `TLS_MODE=corp` это корпоративный CA, при
  `TLS_MODE=internal` — `caddy-root.crt` с ВМ (`/opt/llm/deploy/caddy-root.crt`). Доверие
  корню Caddy задаётся для приложения, а не для всей системы:
  - Python (openai/httpx):
    `OpenAI(..., http_client=httpx.Client(verify="caddy-root.crt"))`. `SSL_CERT_FILE`
    заменяет хранилище всего процесса целиком, поэтому, если приложение ходит и на
    другие HTTPS-адреса, склейте `certifi` и корень: `cat "$(python -m certifi)"
    caddy-root.crt > bundle.pem`;
  - Node.js: `NODE_EXTRA_CA_CERTS=/path/caddy-root.crt` (добавляет корень к стандартным);
  - Java: `keytool -importcert -alias llm-caddy -file caddy-root.crt -keystore app-truststore.jks`
    и `-Djavax.net.ssl.trustStore=...`;
  - curl: `--cacert caddy-root.crt`.
- Ответ HTTP 429 означает, что исчерпан лимит ключа. Повторите запрос позже.

### 3.3 Мониторинг

Нужен профиль `monitoring`. Grafana открывается на `http://localhost:3000` через
туннель из 2.7, логин `admin`, пароль — `GRAFANA_ADMIN_PASSWORD`. Дашборд «LLM»:
- загрузка GPU и VRAM;
- запросы vLLM: в работе и в очереди;
- TTFT p50/p95;
- заполненность KV-кеша;
- токены/с;
- запросы и токены Bifrost по ключам.

На что смотреть:
- очередь vLLM постоянно больше 0 и KV-кеш около 100% — не хватает ёмкости
  (`design.md` §5, §13);
- рост TTFT при той же нагрузке — деградация, смотрите логи;
- температура и загрузка GPU у предела — сообщите администратору железа.

### 3.4 Логи и диагностика

```bash
make ps
make logs SERVICE=vllm                   # или bifrost, caddy, vllm-embed
nvidia-smi
```

## 4. Изменения и обслуживание

### 4.1 Смена модели

1. В `.env` поменять `MODEL_ID` (например, откат на `Qwen/Qwen3.6-27B-FP8`).
2. Предзагрузить веса: `make preload` (2.8), при `HF_HUB_OFFLINE=1` команда всё равно
   работает.
3. `make up`, дождаться healthy, проверить ёмкость KV-кеша в логе.
4. Запустить `make smoke-model` (или `make smoke KEY=...` после этапа 2). Алиас
   `default` не меняется, клиентам ничего делать не нужно.

Если модель тяжелее текущей, пересчитайте бюджет VRAM (`design.md` §5) и при
необходимости отключите профиль `embeddings`.

### 4.2 Обновление образов (vLLM, Bifrost, Caddy и др.)

Сначала изменение вносится в `design.md`, затем в `docker-compose.yml`. Образы
пинятся по точному тегу, `latest` запрещён.

1. Сделать бэкап `bifrost-data` (4.4).
2. Поменять тег в `docker-compose.yml`, выполнить
   `sudo docker compose -f deploy/docker-compose.yml pull` и `make up`.
3. Прогнать `make smoke-model` и `make bench`, сравнить с базовыми цифрами.
4. При регрессии вернуть прежний тег и повторить `make up`.

### 4.3 Ротация секретов

| Что | Как |
|---|---|
| Ключ клиента | `keys.py revoke <id>` + `keys.py create`, передать новый ключ |
| `VLLM_API_KEY` | Новое значение в `.env`, затем `make up` |
| Пароль админки Bifrost | Новое значение в `.env`, затем `make up` **[ВМ]**: убедиться, что новый пароль применился |
| Пароль Grafana | Новое значение в `.env`, затем `make up` |
| TLS-сертификат (`corp`) | Заменить файлы в `deploy/certs/`, `make gateway` (проверит сертификат) и `sudo docker compose -f deploy/docker-compose.yml restart caddy`; срок действия — из заявки DevOps |
| TLS (`internal`) | Серверный сертификат продлевается сам; корень действует 10 лет. При потере `llm_caddy-data` появится новый корень: снова раздать `caddy-root.crt` клиентам |
| Переход `internal` → `corp` | Положить файлы (2.5), `make gateway TLS_MODE=corp`, `sudo docker compose -f deploy/docker-compose.yml restart caddy`; клиенты должны доверять корпоративному CA |
| `BIFROST_ENCRYPTION_KEY` | Не менять без миграции базы Bifrost |

### 4.4 Резервное копирование и восстановление

Критичные данные хранятся в volume `llm_bifrost-data`:
- `config.db` — ключи, лимиты, admin-auth;
- `logs.db` — логи запросов.

При `TLS_MODE=internal` в бэкап входит и `llm_caddy-data`: там корень CA с приватным ключом,
храните его как секрет. Кроме того, сохраните `deploy/.env` в хранилище секретов.

Volume с моделями (`llm_hf-cache`) в бэкап не включается: веса можно скачать заново.

Ежедневный бэкап выполняет DevOps в рамках общего регламента ВМ. Ручной бэкап,
например перед обновлением **[ВМ]**:

```bash
cd /opt/llm/deploy
docker compose stop bifrost    # согласованная копия SQLite; клиенты получат ошибку на несколько секунд
docker run --rm -v llm_bifrost-data:/data:ro -v "$PWD":/backup busybox:1.37.0 \
  tar czf /backup/bifrost-data-$(date +%F).tgz -C /data .
docker compose start bifrost
```

Восстановление:

```bash
docker compose stop bifrost
docker run --rm -v llm_bifrost-data:/data -v "$PWD":/backup busybox:1.37.0 \
  sh -c 'rm -rf /data/* && tar xzf /backup/bifrost-data-YYYY-MM-DD.tgz -C /data'
docker compose start bifrost
```

При восстановлении в `.env` должен стоять тот же `BIFROST_ENCRYPTION_KEY`, что и при
создании бэкапа. Для `llm_caddy-data` те же команды: сервис `caddy`, файл `caddy-data-*.tgz`.

## 5. Типовые проблемы

| Симптом | Причина и действие |
|---|---|
| `up` падает: `required variable ... is missing` | Не заполнена переменная в `.env`; все, кроме `COMPOSE_PROFILES`, `LLM_HOSTNAME` и `TLS_MODE`, обязательны |
| `install_host.sh`: «другой драйвер NVIDIA» или «конфликтующие пакеты» | Выполнить команду из «что делать» (удалить старый драйвер или `docker.io`), затем повторить скрипт |
| `install.sh`: ошибка сертификата (просрочен, не то имя, ключ не подходит) | Проверить пару файлов в `deploy/certs/`; если сертификата нет — `make gateway TLS_MODE=internal` (1.1) |
| `install.sh`: «LLM_HOSTNAME не задан» на этапе gateway | Передать `make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=...` (2.6) |
| caddy не стартует: `File to import not found: tls-...` | Неверный `TLS_MODE` в `.env`; допустимы `corp` и `internal` |
| caddy (`corp`): `no such file` для `/certs/...` | Нет `fullchain.pem`/`privkey.pem` в `deploy/certs/` |
| vllm `unhealthy` при первом старте | Веса не были предзагружены (2.8) или загрузка идёт медленно; `make logs SERVICE=vllm`; после загрузки — снова `make up` |
| vLLM: `CUDA out of memory` при старте | Уменьшить `GPU_MEM_UTIL` или `MAX_MODEL_LEN`; при профиле `embeddings` — проверить сумму долей ≤ 0.92 |
| vllm-embed не стартует | Нехватка памяти в 0.06 или неверный runner — лог `make logs SERVICE=vllm-embed`; временно убрать `embeddings` из `COMPOSE_PROFILES` |
| Клиент: `CERTIFICATE_VERIFY_FAILED` | `corp`: у клиента нет корпоративного CA или в `fullchain.pem` нет промежуточного сертификата. `internal`: клиенту не передан `caddy-root.crt` (3.2) или корень сменился после потери `llm_caddy-data`. Клиент обращается по IP, а не по `LLM_HOSTNAME` |
| Клиент: 401/403 | Нет ключа, ключ неверный или отозван (`make keys`) |
| Клиент: 404 | Путь не начинается с `/v1/`; Caddy пропускает только `/v1/*` |
| Клиент: 429 | Исчерпан лимит ключа; поднять лимит в UI Bifrost или выдать отдельный ключ |
| Клиент: 500 при `reasoning_effort` | Передан `high`; допустимы `low`, `medium`, `xhigh` |
| Клиент: ошибка модели / `model not found` | Имя модели не `default` (или не `embeddings` при выключенном профиле) |
| Медленные ответы | Запросы без `reasoning_effort: "low"` (по умолчанию `xhigh`); очередь vLLM в Grafana |
| `keys.py`: 401 | Не заданы или неверны `BIFROST_ADMIN_USERNAME`/`BIFROST_ADMIN_PASSWORD` |
| `keys.py`: соединение отклонено | Bifrost не запущен — не пройден этап 2 или стек лежит (`make ps`); с рабочей станции — не открыт SSH-туннель на 8080 |
| `make smoke-model`: соединение отклонено | Стек не запущен (`make ps`) или порт vLLM занят другим процессом |

## 6. Разработка проекта

Локально GPU нет. Изменения проверяются без запуска модели:

```bash
make check      # docker compose config для всех профилей, ruff, mypy, pytest, shellcheck
```

Правила (подробно — `CLAUDE.md`):
- сначала `design.md`, потом код;
- образы пинятся по тегу;
- наружу публикуется только 443;
- секреты хранятся только в `.env`;
- всё, что требует GPU, помечается «проверить на ВМ».

Для задач в Claude Code есть агенты:
- `infra-engineer` — `deploy/`;
- `scripts-engineer` — `scripts/`;
- `reviewer` — ревью, только читает.
