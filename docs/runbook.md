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
| `scripts/` | `install_host.sh`, `install.sh`, `preflight.sh`, `smoke_test.py`, `keys.py`, `bench.sh`, тесты (uv-проект) |
| `docs/` | дизайн, заявка DevOps, этот runbook |
| `.claude/agents/` | агенты для разработки: `infra-engineer`, `scripts-engineer`, `reviewer` |

Compose-профили: базовый (caddy, bifrost, vllm), `embeddings` (vllm-embed),
`monitoring` (dcgm-exporter, Prometheus, Grafana). Имя проекта фиксировано — `llm`,
поэтому volumes называются `llm_hf-cache`, `llm_bifrost-data`, `llm_caddy-data`,
`llm_prometheus-data`.

## 1. Перед установкой

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

| | `--tls corp` (основной вариант) | `--tls internal` (сертификата нет) |
|---|---|---|
| Что нужно | `fullchain.pem` + `privkey.pem` от корпоративного CA | Ничего: Caddy сам создаёт CA и выпускает сертификат |
| Доверие клиентов | Уже есть: корпоративный CA распространён через GPO | Корневой `deploy/caddy-root.crt` раздаётся серверам приложений и UI (3.2) |
| Продление | Вручную, по сроку сертификата (4.3) | Автоматически; корень действует 10 лет |
| Что бэкапить | Ничего сверх `bifrost-data` | Ещё volume `llm_caddy-data`, в нём ключ корня (4.4) |

**Если корпоративного сертификата нет**, ставьте с `--tls internal`: сервис заработает
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

Кратко (все команды на ВМ):

```bash
sudo apt-get install -y git
sudo git clone <репозиторий> /opt/llm && cd /opt/llm
sudo bash scripts/install_host.sh           # 2.1; если попросит — sudo reboot и снова cd /opt/llm
sudo bash scripts/preflight.sh              # 2.2; дальше только при «ИТОГ: PASS»
# 2.3, только для --tls corp: положить deploy/certs/fullchain.pem и privkey.pem
sudo bash scripts/install.sh --hostname llm.<корп.домен> --tls corp --profiles monitoring   # 2.4
# или без корпоративного сертификата:
sudo bash scripts/install.sh --hostname llm.<корп.домен> --tls internal --profiles monitoring
```

Затем проверка сервиса (2.5) и бенчмарк (2.6). Путь `/opt/llm` не обязателен:
`install.sh` пропишет в systemd-юнит фактический путь к клону.

### 2.1 Подготовка ВМ: `install_host.sh`

```bash
sudo bash scripts/install_host.sh
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
sudo bash scripts/preflight.sh
```

Что проверяется:
- GPU и объём VRAM;
- драйвер ≥ 580 с open kernel modules;
- Docker ≥ 27 и запуск контейнера с `--gpus all`;
- место на диске, RAM, vCPU;
- доступ к Hugging Face: реальная загрузка `config.json` и первого КиБ весов через Xet CDN;
- доступ к реестрам образов, NTP, наличие `uv`.

Для каждой проваленной проверки скрипт выводит, что делать. Если есть провалы, код
выхода 1. `install.sh` сам запускает preflight первым шагом, отдельный запуск нужен,
чтобы исправить окружение до установки.

### 2.3 Сертификат (только `--tls corp`)

```bash
sudo install -d -m 700 /opt/llm/deploy/certs
sudo install -m 644 fullchain.pem /opt/llm/deploy/certs/fullchain.pem
sudo install -m 600 privkey.pem  /opt/llm/deploy/certs/privkey.pem
```

В `fullchain.pem` должна быть вся цепочка, включая промежуточный CA. Каталог `certs/`
в `.gitignore`. `install.sh` проверит, что сертификат не просрочен, выписан на
`--hostname` и соответствует ключу. Если до истечения меньше 30 дней, выведет
предупреждение.

### 2.4 Установка сервиса: `install.sh`

```bash
sudo bash scripts/install.sh --hostname llm.<корп.домен> --tls corp|internal [--profiles monitoring]
```

| Аргумент | Значение |
|---|---|
| `--hostname` | DNS-имя сервиса |
| `--tls` | `corp` или `internal` (1.1) |
| `--profiles` | пусто (по умолчанию), `monitoring`, `embeddings`, `embeddings,monitoring` |
| `--skip-preflight` | не запускать preflight (при повторной установке) |

Что делает скрипт, по шагам:
1. Запускает `preflight.sh`.
2. Создаёт `deploy/.env` (root, 600) и генерирует пустые секреты: `VLLM_API_KEY`,
   `BIFROST_ADMIN_PASSWORD`, `BIFROST_ENCRYPTION_KEY`, `GRAFANA_ADMIN_PASSWORD`. Логин
   админки Bifrost — `admin`. Уже заданные значения не меняются.
3. Для `corp` проверяет сертификат (2.3).
4. Выполняет `docker compose config` и `pull`.
5. Скачивает веса моделей в volume `llm_hf-cache`. Это ~30 ГБ, занимает десятки минут.
   Отдельный шаг нужен, потому что загрузка внутри запуска могла бы не уложиться в
   `start_period` healthcheck vLLM.
6. Устанавливает и включает systemd-юнит `llm-stack`, запускает стек и ждёт, пока все
   сервисы станут healthy. Пока vLLM загружает модель, логи смотрите в другом терминале:
   `cd /opt/llm/deploy && sudo docker compose logs -f vllm`.
7. Для `internal` выгружает корневой сертификат в `deploy/caddy-root.crt`.
8. Проверяет через 443, что запрос к модели без ключа получает 401/403, а `/` — 404.

Повторный запуск применяет изменения. Аргументы, переданные явно, перезаписывают
значения в `.env`, секреты не трогаются. Остальные параметры (`MODEL_ID`,
`MAX_MODEL_LEN`, `GPU_MEM_UTIL`, `HF_HUB_OFFLINE`, описание — `design.md` §6.1)
правятся в `deploy/.env`, после чего запускается
`sudo bash scripts/install.sh --skip-preflight`. Если используются эмбеддинги,
`GPU_MEM_UTIL` + 0.06 не должно превышать 0.92.

> **Сразу после установки** сохраните копию `BIFROST_ENCRYPTION_KEY` из `deploy/.env`
> вне ВМ, в хранилище секретов заказчика. Без неё базу Bifrost (ключи, лимиты) из
> бэкапа не восстановить, а сменить ключ можно только миграцией.

Критерий приёмки **[ВМ]**: в логе vLLM есть строка
`Maximum concurrency for 65536 tokens per request: N`, и **N ≥ 8**. Если меньше,
добавьте `--kv-cache-dtype fp8` (`design.md` §5).

### 2.5 Проверка сервиса

На ВМ, в root-оболочке. API управления Bifrost доступен на `127.0.0.1:8080` без туннеля:

```bash
sudo -i
cd /opt/llm/scripts
set -a; source <(grep -E '^(BIFROST_ADMIN_USERNAME|BIFROST_ADMIN_PASSWORD|LLM_HOSTNAME)=' ../deploy/.env); set +a

# 1. Ключ для смоук-теста и ключ с маленьким лимитом для проверки 429
uv run keys.py create --name smoke --requests 100 --period 1h
uv run keys.py create --name smoke-limit --requests 3 --period 1h

# 2. Основной смоук-тест через HTTPS. LLM_CA_CERT обязателен: httpx не читает системное хранилище
export LLM_CA_CERT=/opt/llm/deploy/caddy-root.crt     # --tls internal
# export LLM_CA_CERT=/path/to/corp-root-ca.pem        # --tls corp
LLM_API_KEY=sk-bf-... uv run smoke_test.py

# 3. Проверка лимита
LLM_API_KEY=<ключ smoke-limit> uv run smoke_test.py --check-rate-limit 5

# 4. Ключи сохраняются после перезапуска
docker compose -f ../deploy/docker-compose.yml restart bifrost
uv run keys.py list
```

`smoke_test.py` проверяет:
- генерацию текста;
- распознавание изображения;
- `json_schema`;
- tool call;
- отключение мышления через `chat_template_kwargs`;
- 401/403 без ключа и с неверным ключом;
- 404 на `/api/*`, `/metrics` и `/` через 443.

Если какая-то проверка не прошла, сверьтесь с разделом 5. Затем отзовите тестовые
ключи: `uv run keys.py revoke <id>`.

Отдельные проверки при первой установке **[ВМ]**:
- `/v1/*` с ключом `sk-bf-...` работает при включённой admin-auth. По исходникам v2.2.0
  это так; документация Bifrost намекает на `disable_auth_on_inference`. Если не
  работает, добавить этот параметр в `config.json`, предварительно внеся в дизайн;
- модель `default` принимается Bifrost без префикса `vllm/`;
- `reasoning_effort` и `chat_template_kwargs` доходят до vLLM;
- vllm-embed стартует при util 0.06;
- `curl -s http://127.0.0.1:8080/metrics` отвечает без пароля, а
  `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/api/governance/virtual-keys`
  без Basic-auth возвращает 401;
- после `sudo reboot` стек поднимается сам: `cd /opt/llm/deploy && sudo docker compose ps`.

С рабочей станции администратора UI Bifrost и Grafana открываются через туннель:

```bash
ssh -L 8080:127.0.0.1:8080 -L 3000:127.0.0.1:3000 <админ>@<вм>
```

### 2.6 Бенчмарк

```bash
sudo bash /opt/llm/scripts/bench.sh        # 8 параллельных запросов; bench.sh 16 — другая параллельность
```

Бенчмарк гоняет `vllm bench serve` внутри контейнера напрямую против vLLM, без TLS и
gateway: измеряется сама модель. Результаты сохраняются в `scripts/bench-results/`.
Зафиксируйте TTFT и throughput после установки, их берут за базу при обновлениях.

### 2.7 Предзагрузка весов вручную

Используется при смене модели (4.1). То же самое делает шаг 5 `install.sh` **[ВМ]**:

```bash
cd /opt/llm/deploy
sudo docker compose run --rm --no-deps -e HF_HUB_OFFLINE=0 --entrypoint hf vllm \
  download "$(sudo grep '^MODEL_ID=' .env | cut -d= -f2-)"
```

После загрузки можно поставить `HF_HUB_OFFLINE=1`: сервис перестанет обращаться к
Hugging Face.

## 3. Ежедневная эксплуатация

### 3.1 API-ключи

Ключи выпускаются по одному на приложение и по одному на сотрудника (или на
UI-сервер). Команды выполняются на ВМ в root-оболочке с переменными `BIFROST_ADMIN_*`,
как в 2.5.

```bash
cd /opt/llm/scripts
uv run keys.py create --name app-crm --description "CRM, извлечение данных" \
  --requests 600 --tokens 500000 --period 1h     # выводит id и sk-bf-...
uv run keys.py list                              # расход/лимиты, значения ключей не показываются
uv run keys.py revoke <id>                       # деактивация: доступ пропадает сразу
```

Значение `sk-bf-...` показывается один раз, при создании. Передавайте его владельцу
по защищённому каналу. То же можно сделать в UI Bifrost: `http://localhost:8080`
через туннель (2.5).

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
- Клиенту нужен корневой сертификат: при `--tls corp` это корпоративный CA, при
  `--tls internal` — `caddy-root.crt` с ВМ (`/opt/llm/deploy/caddy-root.crt`). Доверие
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
туннель из 2.5, логин `admin`, пароль — `GRAFANA_ADMIN_PASSWORD`. Дашборд «LLM»:
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
cd /opt/llm/deploy
docker compose ps
docker compose logs --since 1h vllm      # или bifrost, caddy, vllm-embed
nvidia-smi
```

## 4. Изменения и обслуживание

### 4.1 Смена модели

1. В `.env` поменять `MODEL_ID` (например, откат на `Qwen/Qwen3.6-27B-FP8`).
2. Предзагрузить веса (2.7), при `HF_HUB_OFFLINE=1` команда всё равно работает.
3. `docker compose up -d vllm`, дождаться healthy, проверить ёмкость KV-кеша в логе.
4. Запустить `smoke_test.py`. Алиас `default` не меняется, клиентам ничего делать не нужно.

Если модель тяжелее текущей, пересчитайте бюджет VRAM (`design.md` §5) и при
необходимости отключите профиль `embeddings`.

### 4.2 Обновление образов (vLLM, Bifrost, Caddy и др.)

Сначала изменение вносится в `design.md`, затем в `docker-compose.yml`. Образы
пинятся по точному тегу, `latest` запрещён.

1. Сделать бэкап `bifrost-data` (4.4).
2. Поменять тег в `docker-compose.yml`, выполнить `docker compose pull && docker compose up -d`.
3. Прогнать `smoke_test.py` и `bench.sh`, сравнить с базовыми цифрами.
4. При регрессии вернуть прежний тег и повторить `up -d`.

### 4.3 Ротация секретов

| Что | Как |
|---|---|
| Ключ клиента | `keys.py revoke <id>` + `keys.py create`, передать новый ключ |
| `VLLM_API_KEY` | Новое значение в `.env`, затем `docker compose up -d vllm bifrost` |
| Пароль админки Bifrost | Новое значение в `.env`, затем `docker compose up -d bifrost` **[ВМ]**: убедиться, что новый пароль применился |
| Пароль Grafana | Новое значение в `.env`, затем `docker compose up -d grafana` |
| TLS-сертификат (`corp`) | Заменить файлы в `deploy/certs/`, `sudo bash scripts/install.sh --skip-preflight` (проверит сертификат) и `docker compose restart caddy`; срок действия — из заявки DevOps |
| TLS (`internal`) | Серверный сертификат продлевается сам; корень действует 10 лет. При потере `llm_caddy-data` появится новый корень: снова раздать `caddy-root.crt` клиентам |
| Переход `internal` → `corp` | Положить файлы (2.3), `sudo bash scripts/install.sh --skip-preflight --tls corp`, `docker compose restart caddy`; клиенты должны доверять корпоративному CA |
| `BIFROST_ENCRYPTION_KEY` | Не менять без миграции базы Bifrost |

### 4.4 Резервное копирование и восстановление

Критичные данные хранятся в volume `llm_bifrost-data`:
- `config.db` — ключи, лимиты, admin-auth;
- `logs.db` — логи запросов.

При `--tls internal` в бэкап входит и `llm_caddy-data`: там корень CA с приватным ключом,
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
| `up` падает: `required variable ... is missing` | Не заполнена переменная в `.env`; все, кроме `COMPOSE_PROFILES`, обязательны |
| `install_host.sh`: «другой драйвер NVIDIA» или «конфликтующие пакеты» | Выполнить команду из «что делать» (удалить старый драйвер или `docker.io`), затем повторить скрипт |
| `install.sh`: ошибка сертификата (просрочен, не то имя, ключ не подходит) | Проверить пару файлов в `deploy/certs/`; если сертификата нет — `--tls internal` (1.1) |
| caddy не стартует: `File to import not found: tls-...` | Неверный `TLS_MODE` в `.env`; допустимы `corp` и `internal` |
| caddy (`corp`): `no such file` для `/certs/...` | Нет `fullchain.pem`/`privkey.pem` в `deploy/certs/` |
| vllm `unhealthy` при первом старте | Веса не были предзагружены (2.7) или загрузка идёт медленно; `docker compose logs vllm`; после загрузки — снова `docker compose up -d` |
| vLLM: `CUDA out of memory` при старте | Уменьшить `GPU_MEM_UTIL` или `MAX_MODEL_LEN`; при профиле `embeddings` — проверить сумму долей ≤ 0.92 |
| vllm-embed не стартует | Нехватка памяти в 0.06 или неверный runner — лог `docker compose logs vllm-embed`; временно убрать `embeddings` из `COMPOSE_PROFILES` |
| Клиент: `CERTIFICATE_VERIFY_FAILED` | `corp`: у клиента нет корпоративного CA или в `fullchain.pem` нет промежуточного сертификата. `internal`: клиенту не передан `caddy-root.crt` (3.2) или корень сменился после потери `llm_caddy-data`. Клиент обращается по IP, а не по `LLM_HOSTNAME` |
| Клиент: 401/403 | Нет ключа, ключ неверный или отозван (`keys.py list`) |
| Клиент: 404 | Путь не начинается с `/v1/`; Caddy пропускает только `/v1/*` |
| Клиент: 429 | Исчерпан лимит ключа; поднять лимит в UI Bifrost или выдать отдельный ключ |
| Клиент: 500 при `reasoning_effort` | Передан `high`; допустимы `low`, `medium`, `xhigh` |
| Клиент: ошибка модели / `model not found` | Имя модели не `default` (или не `embeddings` при выключенном профиле) |
| Медленные ответы | Запросы без `reasoning_effort: "low"` (по умолчанию `xhigh`); очередь vLLM в Grafana |
| `keys.py`: 401 | Не заданы или неверны `BIFROST_ADMIN_USERNAME`/`BIFROST_ADMIN_PASSWORD` |
| `keys.py`: соединение отклонено | Bifrost не запущен (`docker compose ps`); с рабочей станции — не открыт SSH-туннель на 8080 |

## 6. Разработка проекта

Локально GPU нет. Изменения проверяются без запуска модели:

```bash
# конфиги (с временным env-файлом вне репозитория)
docker compose -f deploy/docker-compose.yml --env-file /tmp/test.env config
docker compose -f deploy/docker-compose.yml --env-file /tmp/test.env \
  --profile embeddings --profile monitoring config

# скрипты
cd scripts
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
bash -n *.sh && uvx --from shellcheck-py shellcheck *.sh
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
