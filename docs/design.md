# Дизайн-документ: корпоративный LLM-сервис на RTX PRO 5000 72 ГБ

Версия 0.1 — черновик на согласование. Дата: 2026-09-15.

## 1. Цель и рамки

Развернуть на сервере заказчика self-hosted мультимодальную LLM и отдать её
сотрудникам и внутренним приложениям через единый OpenAI-совместимый HTTPS-эндпоинт
с индивидуальными API-ключами, лимитами и учётом использования.

**В рамках:**
- инференс-сервер (vLLM) с одной основной моделью (текст + изображения);
- gateway: API-ключи, лимиты, учёт, метрики;
- TLS-терминация, DNS-имя;
- мониторинг GPU и сервиса;
- опционально: модель эмбеддингов на той же карте;
- документация для эксплуатации и заявка DevOps на сетевые настройки.

**Вне рамок:** веб-чат (Open WebUI и т. п.), RAG-приложения, SSO — живут на других
серверах и подключаются к API как обычные клиенты. Отказоустойчивость (второй GPU /
второй сервер) не требуется.

## 2. Исходные данные

| Параметр | Значение |
|---|---|
| GPU | NVIDIA RTX PRO 5000 Blackwell, 72 ГБ GDDR7 ECC, compute capability 12.0 (sm_120), 300 Вт |
| Хост | Сервер заказчика, ВМ с GPU passthrough |
| ОС ВМ | Ubuntu 26.04 LTS на ВМ заказчика (поддерживаются 22.04/24.04/26.04; RAM, CPU, диск — уточнить) |
| Драйвер / Docker | Не проверены — есть чек-лист (раздел 10) |
| Сеть | Доступ только через корпоративный VPN; у ВМ есть прямой выход в интернет |
| Пользователи | Сотрудники (через UI на других серверах) и внутренние приложения |
| Нагрузка | Не оценена; закладываем ~10 одновременных запросов, контекст до 64k |
| Auth | API-ключи для всех клиентов; SSO не нужен |
| Доступ к API | HTTPS, сертификат корпоративного CA, внутреннее DNS-имя |

## 3. Ключевые решения

### 3.1 Модель: Qwen3.8-27B-FP8

| Критерий | Qwen3.8-27B-FP8 (выбор) | Gemma 4 31B-it (запасной) |
|---|---|---|
| Зрение (сканы, фото) | Да, нативно; OmniDocBench 91.1 | Да |
| Лицензия | Apache 2.0 | Apache 2.0 |
| Веса на GPU | ~28 ГБ (официальный FP8) | ~62 ГБ в BF16, официального FP8 нет |
| Остаётся под KV-кеш | ~30 ГБ | ~3–5 ГБ — мало для параллельных запросов |
| Контекст | 262k нативно | 256k |
| Русский язык | Сильный (семейство Qwen) | 140+ языков |
| Дата выхода | Август 2026 | Июль 2026 |

Почему не крупнее: Qwen3.8-Max (2.4T), Qwen3.5-122B-A10B и Mistral Small 4
(119B-A6B) в FP8 не помещаются в 72 ГБ; NVFP4-варианты влезают, но на sm_120
работают медленнее FP8 и оставляют мало памяти под KV-кеш. Dense 27B в FP8 — лучший
баланс качества, скорости и параллельности для одной карты.

Особенности Qwen3.8, которые нужно учесть в API-документации для клиентов:
- «мышление» включено по умолчанию, уровень `reasoning_effort` по умолчанию `xhigh`;
  допустимы только `xhigh | medium | low`, значение `high` даёт HTTP 500;
- для приложений (извлечение данных, классификация) рекомендовать
  `reasoning_effort: "low"` или `chat_template_kwargs: {"enable_thinking": false}`.

Решение по модели легко обратимо: смена модели = изменение одной переменной в `.env`
и перезапуск контейнера.

### 3.2 Движок: vLLM (подтверждаем)

vLLM — стандарт для продакшн-инференса: OpenAI-совместимый API, continuous batching,
prefix caching, structured outputs, tool calling, метрики Prometheus. Официальный
образ `vllm/vllm-openai:v0.29.0` (сентябрь 2026) собран с CUDA 13.0 и включает
sm_120 в список архитектур, поэтому кастомные/nightly-сборки для Blackwell больше не
нужны. Требование: драйвер NVIDIA ≥ 580 (open kernel modules — для Blackwell
обязательны).

Альтернативы (SGLang, TensorRT-LLM) не дают преимуществ для одной карты и одной
модели, а TensorRT-LLM заметно сложнее в сопровождении.

### 3.3 Gateway: Bifrost (вместо LiteLLM)

vLLM поддерживает только один API-ключ (`--api-key`), поэтому нужен gateway.
LiteLLM — самый известный вариант, но в 2026 году пережил компрометацию пакетов на
PyPI (март) и серию CVE, включая SQL-инъекцию в проверке ключей и RCE (CVSS 10.0).
Для контура, где gateway — единственная точка входа, это неприемлемый профиль риска.

Выбор — **Bifrost** (maximhq/bifrost, Apache 2.0, Go, один бинарник):
- virtual keys с лимитами (запросы/токены в час) и бюджетами; создание через UI или
  REST (`POST /api/governance/virtual-keys`);
- OpenAI-совместимый `/v1/*`, встроенный провайдер `vllm` (`vllm_key_config.url`
  на `http://vllm:8000`);
- Prometheus-метрики на `/metrics`, учёт по ключу/модели;
- SQLite (`config_store`, файл в volume `bifrost-data`), без Postgres/Redis — минимум
  компонентов.

Конфигурация Bifrost (`deploy/bifrost/config.json`):
- `client.enforce_auth_on_inference: true` — запрос к `/v1/*` без действующего
  virtual key отклоняется (по умолчанию Bifrost пропускает без ключа);
- `config_store` на SQLite — ключи и лимиты переживают перезапуск и попадают в бэкап;
- admin-auth (`governance.auth_config`) включена: логин/пароль из
  `BIFROST_ADMIN_USERNAME` / `BIFROST_ADMIN_PASSWORD`, `/metrics` — без auth (его
  читает Prometheus). Без неё API управления ключами доступен любому контейнеру в
  docker-сетях;
- `BIFROST_ENCRYPTION_KEY` — шифрование ключей в SQLite, в том числе в бэкапах. Задаётся
  до первого старта; копия ключа хранится вне ВМ, без неё бэкап не восстановить.

Админ-интерфейс Bifrost наружу не публикуется: доступ только через SSH-туннель на
`localhost:8080`. Снаружи через Caddy проксируется только `/v1/*`.

### 3.4 TLS и точка входа: Caddy

Caddy терминирует TLS, проксирует `/v1/*` на Bifrost, всё остальное — 404. Единственный
порт наружу — 443. Режим TLS задаётся переменной `TLS_MODE`:

- **`corp`** (основной) — сертификат корпоративного CA: `deploy/certs/fullchain.pem`
  (цепочка) и `deploy/certs/privkey.pem`. Клиенты уже доверяют корпоративному CA.
- **`internal`** (если корпоративный сертификат выпустить нельзя) — собственный CA Caddy
  (`tls internal`). Корневой сертификат (срок 10 лет) и его ключ хранятся в volume
  `caddy-data`; серверный сертификат на `LLM_HOSTNAME` короткоживущий и продлевается
  автоматически, без интернета и без участия DevOps. При установке корень выгружается в
  `deploy/caddy-root.crt` и раздаётся клиентам — серверам приложений и UI (сотрудники
  ходят через UI, поэтому клиентов немного).
  - Доверие корню задаётся на уровне приложения (`verify=`/`SSL_CERT_FILE`,
    `NODE_EXTRA_CA_CERTS`, truststore Java), а не в системном хранилище: ключ корня
    лежит на ВМ, и системное доверие распространилось бы на любые домены.
  - Потеря `caddy-data` означает новый корень и повторную раздачу, поэтому volume
    включается в бэкап.
  - Переход на `corp` позже: положить файлы в `certs/`, сменить `TLS_MODE`,
    `docker compose up -d caddy`; клиенты должны доверять корпоративному CA.

Отвергнутые альтернативы для случая без корпоративного сертификата: Let's Encrypt с
DNS-01 (нужны публичная DNS-зона с API, своя сборка Caddy с плагином, имя хоста
попадает в публичные CT-логи); самоподписанный сертификат без CA (нет автопродления,
на практике клиенты отключают проверку); HTTP внутри VPN (ключи `sk-bf-` в открытом виде).

### 3.5 Мониторинг: Prometheus + Grafana + DCGM

Опциональный compose-профиль `monitoring`: dcgm-exporter (GPU: загрузка, VRAM,
температура), метрики vLLM (очередь, TTFT, throughput, заполненность KV-кеша),
метрики Bifrost (запросы по ключам). Grafana с преднастроенными дашбордами.

### 3.6 Эмбеддинги: опциональный профиль на той же карте

Второй контейнер vLLM с `Qwen3-Embedding-0.6B` (или `bge-m3` — лучше на кириллице,
проверить на данных заказчика) и `--gpu-memory-utilization 0.06` (~4 ГБ). Дешевле и
надёжнее отдельной Windows-машины; включается профилем `embeddings`, когда
понадобится RAG. Доступ через тот же gateway, эндпоинт `/v1/embeddings`.

Два экземпляра vLLM на одной карте — штатный сценарий без MIG/MPS: память делится
через `gpu-memory-utilization` (доля от общей памяти карты у каждого), вычисления —
по времени, что для модели на 0.6B незаметно. Условия:
- сумма долей ≤ 0.92 (запас на CUDA-контексты обоих процессов и фрагментацию);
- строго последовательный старт: контейнер эмбеддингов запускается только после
  healthcheck основного (`depends_on: condition: service_healthy`), иначе
  профилировочный прогон одного может «увидеть» память, которую забирает другой;
- при смене основной модели на более тяжёлую бюджет из раздела 5 пересчитывается,
  профиль эмбеддингов при необходимости отключается;
- `--max-model-len 8192`: при дефолтных 32k KV-кеш одной последовательности (~3,5 ГБ)
  вместе с весами не помещается в 0.06; `--runner pooling` — чтобы vLLM не поднял
  модель как генеративную; алиас `--served-model-name embeddings`.
MIG на этой карте есть, но фиксированные слайсы отняли бы у LLM больше памяти, чем
нужно эмбеддингам; изоляция того не стоит.

### 3.7 Два этапа развёртывания: сначала модель, потом внешний доступ

Внешний доступ зависит от DevOps (DNS-имя, сертификат корпоративного CA, правила
файрвола), а модель — нет. Поэтому стек развёртывается двумя независимыми этапами:

| Этап | Сервисы | Что нужно заранее | Как проверяется |
|---|---|---|---|
| 1. Модель | `vllm` (+ `vllm-embed`, `monitoring`) | ВМ с GPU и доступ к Hugging Face | `smoke_test.py --direct` и `bench.sh` на `127.0.0.1:8000` |
| 2. Внешний доступ | `caddy`, `bifrost` — профиль `gateway` | DNS-имя, сертификат (или `TLS_MODE=internal`), открытый 443 | `smoke_test.py` через `https://${LLM_HOSTNAME}/v1` |

Механика: `caddy` и `bifrost` вынесены в compose-профиль `gateway`. Первый этап поднимает
только vLLM, и `LLM_HOSTNAME` / `TLS_MODE` на нём не нужны — они проверяются лишь тогда,
когда профиль `gateway` включён. Этап задаётся флагом `scripts/install.sh --stage
model|gateway`, который управляет наличием профиля в `COMPOSE_PROFILES`; `--stage gateway`
добавляет профиль, и дальше он остаётся в `.env` (выключить — ручной правкой).

Чтобы модель можно было проверить и отбенчить до появления gateway, vLLM публикует порт
**только на `127.0.0.1:8000`**: снаружи ВМ он по-прежнему недоступен (§8) и требует
`VLLM_API_KEY`. Доступ с рабочей станции — через SSH-туннель, как у Bifrost и Grafana.

Смоук-тест на первом этапе (`--direct`) прогоняет те же проверки модели (текст, зрение,
`json_schema`, tool call, `enable_thinking`), пропуская проверку путей, закрытых Caddy:
на этом этапе Caddy ещё нет. Так регрессии модели ловятся до второго этапа, а второй
этап проверяет уже только обвязку.

## 4. Архитектура

```
 VPN-клиенты (сотрудники через UI, приложения на других серверах)
        │  HTTPS :443   Authorization: Bearer sk-bf-...
        ▼
 ┌────────────────────────── ВМ (Ubuntu, GPU passthrough) ──────────────────────────┐
 │  Caddy (:443, TLS corp CA | internal CA) — только /v1/* → bifrost:8080     [gw]  │
 │        │                                                                         │
 │  Bifrost (:8080, localhost only) — virtual keys, лимиты, учёт, /metrics    [gw]  │
 │        │  provider "vllm" base_url=http://vllm:8000, ключ VLLM_API_KEY           │
 │        ├──────────────────────────────┐                                          │
 │  vLLM main (:8000, +127.0.0.1) vLLM embed (:8001, internal, профиль embeddings)  │
 │  Qwen3.8-27B-FP8, util 0.85    Qwen3-Embedding-0.6B, util 0.06                   │
 │        └──────────────┬───────────────┘                                          │
 │                 RTX PRO 5000 72 ГБ                                               │
 │                                                                                  │
 │  monitoring: dcgm-exporter → Prometheus (:9090) → Grafana (127.0.0.1:3000)       │
 │  volumes: hf-cache, bifrost-data (SQLite), caddy-data, prometheus-data           │
 └──────────────────────────────────────────────────────────────────────────────────┘
```

Docker-сети: `edge` (caddy ↔ bifrost) и `inference` (bifrost ↔ vllm). `[gw]` —
сервисы compose-профиля `gateway`, второго этапа развёртывания (§3.7). vLLM и Bifrost
публикуют порты только на `127.0.0.1` (8000 и 8080), наружу ВМ смотрит один 443.

## 5. Бюджет видеопамяти (72 ГБ)

| Потребитель | Оценка |
|---|---|
| Веса Qwen3.8-27B-FP8 (+ vision encoder) | ~28 ГБ |
| Активации, CUDA-графы, буферы vLLM | ~4 ГБ |
| KV-кеш основной модели (остаток при util 0.85 ≈ 61 ГБ) | ~29 ГБ |
| vLLM embeddings (util 0.06, опционально) | ~4 ГБ |
| Резерв драйвера/дисплея/фрагментации | ~4–7 ГБ |

Реальная ёмкость KV-кеша в токенах видна в логе vLLM при старте
(«Maximum concurrency for N tokens per request»). Критерий приёмки: ≥ 8 параллельных
запросов при `max-model-len` 65536. Если не хватает — включить
`--kv-cache-dtype fp8` (удваивает ёмкость) до смены модели.

## 6. Параметры vLLM (стартовые)

```
vllm serve Qwen/Qwen3.8-27B-FP8
  --served-model-name default
  --max-model-len 65536
  --gpu-memory-utilization 0.85
  --max-num-seqs 32
  --limit-mm-per-prompt '{"image": 8}'
  --reasoning-parser qwen3
  --enable-auto-tool-choice --tool-call-parser qwen3_coder
  --enable-prefix-caching
# внутренний ключ — через переменную окружения VLLM_API_KEY (vLLM читает её сам),
# а не флагом --api-key: так он не виден в списке процессов хоста
```

- `default` — единственный и стабильный алиас: клиенты не меняют код при смене модели;
  имя конкретной модели в алиасе не используется, чтобы не вводить в заблуждение после
  смены `MODEL_ID`.
- Контекст 64k покрывает ~100 страниц текста или несколько сканов; расширить до
  262k можно переменной, но это уменьшит параллельность.
- Structured outputs (`response_format: json_schema`) включены в vLLM по умолчанию.
- Флаги парсеров те же, что рекомендованы для Qwen3.6/3.8; проверить на смоук-тесте.

### 6.1 Переменные `deploy/.env`

| Переменная | Секрет | Назначение |
|---|---|---|
| `COMPOSE_PROFILES` | — | Профили сверх vLLM: `gateway` (второй этап, §3.7), `embeddings`, `monitoring` (читает и systemd-юнит) |
| `LLM_HOSTNAME` | — | DNS-имя; клиентский `base_url = https://${LLM_HOSTNAME}/v1`. Нужен на втором этапе (§3.7) |
| `TLS_MODE` | — | `corp` — файлы в `deploy/certs/`; `internal` — CA Caddy (§3.4). Нужен на втором этапе |
| `MODEL_ID` | — | HF-идентификатор основной модели |
| `MAX_MODEL_LEN` | — | Контекст основной модели (65536) |
| `GPU_MEM_UTIL` | — | Доля VRAM основной модели (0.85); вместе с 0.06 эмбеддингов ≤ 0.92 (§3.6) |
| `HF_HUB_OFFLINE` | — | 1 — запрет обращений к Hugging Face после загрузки |
| `EMBED_MODEL_ID` | — | Модель профиля `embeddings` |
| `VLLM_API_KEY` | да | Внутренний ключ Bifrost → vLLM |
| `BIFROST_ADMIN_USERNAME`, `BIFROST_ADMIN_PASSWORD` | да | Admin-auth Bifrost (UI и REST управления) |
| `BIFROST_ENCRYPTION_KEY` | да | Шифрование SQLite Bifrost; копия — вне ВМ |
| `GRAFANA_ADMIN_PASSWORD` | да | Пароль admin Grafana |

## 7. Доступ, ключи и клиенты

- Один ключ на приложение и один на каждого сотрудника (или на UI-сервер, если UI
  сам ведёт пользователей). Ключи создаются админом в Bifrost с лимитами
  (запросов/час, токенов/час) и опциональным бюджетом.
- Клиенты используют стандартные OpenAI SDK: `base_url=https://llm.<домен>/v1`,
  `api_key=sk-bf-...`, `model="default"`.
- Ротация: ключ отзывается в Bifrost мгновенно — деактивацией (`is_active: false`),
  а не удалением: доступ пропадает сразу, запись остаётся для аудита; внутренний
  `VLLM_API_KEY` меняется через `.env` + перезапуск.
- Учёт: метрики Bifrost по ключу в Grafana; при необходимости — экспорт логов.

## 8. Безопасность

- Наружу только 443; 22 — только для админов (решает DevOps).
- vLLM публикует порт только на `127.0.0.1:8000` (смоук-тест и бенчмарк модели на
  первом этапе, §3.7) и требует внутренний ключ; снаружи ВМ он недоступен.
- Bifrost UI/API управления — только `127.0.0.1`, доступ через SSH-туннель, с
  admin-auth (логин/пароль).
- Grafana — только `127.0.0.1:3000`, доступ через SSH-туннель; порт 3000 наружу не
  открывается.
- Ключи в SQLite Bifrost зашифрованы `BIFROST_ENCRYPTION_KEY`.
- Секреты (§6.1) — в `.env`, не в репозитории; `.env.example` с пустыми значениями.
- Образы пинятся по версии (не `latest`); обновление — осознанная операция.
- Резервное копирование `bifrost-data` (ключи, лимиты) и, в режиме `TLS_MODE=internal`,
  `caddy-data` (корень CA с ключом — бэкап хранить как секрет) — ежедневно, тем же
  механизмом, что и остальные ВМ заказчика (вопрос DevOps).
- Модели скачиваются с Hugging Face по HTTPS в volume `hf-cache`; после первой
  загрузки интернет для работы не нужен (`HF_HUB_OFFLINE=1` — по желанию).

## 9. Эксплуатация

Типовые операции завёрнуты в `Makefile` в корне репозитория (`make help` — список);
он вызывает те же скрипты и `docker compose`, ничего своего не делает.

| Операция | Действие |
|---|---|
| Установка, этап 1 (модель) | `make host` (драйвер, Docker, Toolkit, uv, NTP) → перезагрузка → `make preflight` → `make model` = `install.sh --stage model` (`.env` с генерацией секретов, предзагрузка весов, systemd, запуск, проверка 401 и ответа модели на `127.0.0.1:8000`) |
| Установка, этап 2 (внешний доступ) | `make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp\|internal` = `install.sh --stage gateway` (проверка сертификата, профиль `edge`, запуск Caddy и Bifrost, проверка 401/404 через 443) |
| Проверка модели | `make smoke-model` — `smoke_test.py --direct` напрямую к vLLM (без TLS и gateway); после этапа 2 — `make smoke KEY=sk-bf-...` через 443 |
| Первый запуск / смена модели | Сначала предзагрузить веса в volume `hf-cache` отдельной командой, затем `up -d`: иначе скачивание ~30 ГБ может не уложиться в `start_period` healthcheck vLLM |
| Запуск / остановка | `docker compose up -d` / `down`; unit systemd (`deploy/systemd/`) для автозапуска |
| Смена модели | `MODEL_ID` в `.env`, `docker compose up -d vllm`; алиас `default` сохраняется |
| Новый ключ | Bifrost UI через SSH-туннель или `scripts/keys.py create --name app-x --requests 600 --tokens 500000 --period 1h` |
| Отзыв ключа | `scripts/keys.py revoke <id>` (деактивация) |
| Обновление vLLM | Изменить тег образа, прогнать `scripts/smoke_test.py`, откатить при регрессии |
| Диагностика | Grafana; `docker compose logs vllm`; `nvidia-smi` |
| Бенчмарк | `make bench` (`scripts/bench.sh`) — `vllm bench serve` внутри контейнера против vLLM напрямую (без TLS и gateway): фиксируем TTFT и throughput модели после первого этапа |

## 10. Требования к ВМ и чек-лист подготовки

Требования: Ubuntu LTS 24.04, 26.04 или 22.04, ≥ 64 ГБ RAM (загрузка 28 ГБ весов идёт
через RAM), ≥ 8 vCPU, ≥ 300 ГБ SSD (образы ~20 ГБ, модели 30–60 ГБ, логи, запас на вторую
модель), время по NTP.

На 26.04 (`resolute`) есть всё, что ставит `install_host.sh`: `nvidia-driver-580-server-open`
(580.178.04) в репозиториях Ubuntu, `docker-ce` 29.x на `download.docker.com`, репозиторий
NVIDIA Container Toolkit не зависит от выпуска. Связка драйвер + Toolkit на 26.04 не
проверялась вживую — проверяем на ВМ через `make preflight`.

Подготовку автоматизирует `scripts/install_host.sh` (идемпотентный, запускается от root):
драйвер `nvidia-driver-580-server-open` (ветка закреплена именем пакета), Docker Engine из
`download.docker.com` (проверяется версия ≥ 27; патчи безопасности приходят через apt),
NVIDIA Container Toolkit из `nvidia.github.io` с `nvidia-ctk runtime configure`, `uv`
закреплённой версии в `/usr/local/bin`, включение NTP. Чужой драйвер NVIDIA или пакет
`docker.io` скрипт не удаляет — останавливается и говорит, что сделать.

Чек-лист (`scripts/preflight.sh` автоматизирует проверки):
1. `nvidia-smi` видит RTX PRO 5000, 72 ГБ; драйвер ≥ 580, ветка `-open`
   (`nvidia-driver-580-server-open`).
2. Docker Engine ≥ 27 и NVIDIA Container Toolkit; `docker run --rm --gpus all
   nvidia/cuda:13.0.1-base-ubuntu24.04 nvidia-smi` работает.
3. Доступ из ВМ к Hugging Face (реальная загрузка `config.json` модели — веса идут
   через Xet, `*.hf.co`), реестрам образов вместе с их CDN (`registry-1.docker.io`,
   `production.cloudflare.docker.com`, `ghcr.io`, `pkg-containers.githubusercontent.com`,
   `nvcr.io`).
4. Свободное место на диске ≥ 300 ГБ в `/var/lib/docker` или отдельном volume.
5. Установлен `uv` (для `scripts/`).

## 11. Сетевые настройки для согласования с DevOps

Вынесены в отдельный документ `docs/devops-request.md`: DNS-имя, сертификат
корпоративного CA, правила файрвола (443 из VPN-подсетей и с серверов приложений,
22 для админов; Grafana — через SSH-туннель), исходящий доступ к реестрам и Hugging
Face, резервное копирование, требования к ВМ.

## 12. Структура репозитория и план реализации

```
LLM/
├── CLAUDE.md                 правила проекта для агентов
├── README.md                 быстрый старт и ссылки
├── Makefile                  однострочные команды развёртывания и эксплуатации
├── docs/
│   ├── design.md             этот документ
│   ├── devops-request.md     заявка DevOps
│   └── runbook.md            инструкция по установке и эксплуатации
├── deploy/
│   ├── docker-compose.yml    caddy, bifrost, vllm, [vllm-embed], [monitoring]
│   ├── .env.example
│   ├── caddy/Caddyfile
│   ├── bifrost/config.json
│   ├── monitoring/           prometheus.yml, grafana provisioning + dashboards
│   └── systemd/              llm-stack.service (автозапуск)
├── scripts/                  uv-проект: install_host.sh, install.sh, preflight.sh,
│                             smoke_test.py, keys.py, bench.sh, with_env.sh,
│                             api_errors.py (общий разбор ошибок API), tests/
└── .claude/agents/           infra-engineer, scripts-engineer, reviewer
```

План с критериями проверки:

1. **Дизайн и заявка DevOps** (этот этап) → проверка: согласованы модель, gateway,
   сетевые решения.
2. **Compose и конфиги** (агент `infra-engineer`) → проверка: `docker compose config`
   валиден; `reviewer` не находит замечаний по безопасности (порты, секреты, пины).
3. **Скрипты** (агент `scripts-engineer`) → проверка: pytest на `keys.py` и парсинг
   `smoke_test.py`; ruff/mypy чисто.
4. **Установка на ВМ, этап 1 — модель** (`install_host.sh`, `install.sh --stage model`)
   → проверка: `preflight.sh` зелёный; vLLM стартует, лог показывает ёмкость KV-кеша;
   `smoke_test.py --direct` проходит текст + картинку + JSON-схему + tool call на
   `127.0.0.1:8000`; запрос без ключа получает 401.
   **Этап 2 — внешний доступ** (`install.sh --stage gateway`, после DNS-имени и
   сертификата от DevOps) → проверка: `smoke_test.py` проходит те же проверки через
   HTTPS с ключом Bifrost; запрос без ключа и с неверным ключом получает 401/403;
   `/api/*`, `/metrics`, `/` через 443 — 404; ключ с малым лимитом получает 429 при
   превышении; ключ переживает `docker compose restart bifrost`.
5. **Бенчмарк и тюнинг** → проверка: `vllm bench serve` фиксирует TTFT/throughput при
   8 параллельных запросах; при нехватке KV — `--kv-cache-dtype fp8`.
6. **Передача** → runbook, дашборды Grafana, выданные ключи, бэкап `bifrost-data`.

## 13. Риски и открытые вопросы

| Риск / вопрос | Митигация |
|---|---|
| Парсеры `qwen3` / `qwen3_coder` для Qwen3.8 в v0.29.0 могут требовать правки | Смоук-тест на этапе 4; откат на Qwen3.6-27B-FP8 (те же флаги) |
| Качество FP8 на sm_120 / производительность | Бенчмарк на этапе 5; запасной вариант BF16-модели поменьше |
| Хватит ли 64k контекста и ~8 параллельных | Метрики очереди vLLM; `--kv-cache-dtype fp8`; MoE-модель (Qwen3.6-35B-A3B) как быстрый вариант |
| Защита админ-UI Bifrost | Не публикуется наружу; admin-auth включена (§3.3); проверить на ВМ, что `/metrics` доступен Prometheus без auth |
| Bifrost v2.2.0 может не принимать модель `default` без префикса `vllm/` | Первый `curl` на ВМ; при необходимости — алиас в Bifrost или смена контракта §7 |
| Bifrost может отбрасывать нестандартные поля (`chat_template_kwargs`) | Проверить на ВМ; для клиентов рекомендовать `reasoning_effort: "low"` |
| Хранилище логов Bifrost включено по умолчанию (SQLite `logs.db` в `bifrost-data`) и сохраняет промпты и сканы | Открытый вопрос к заказчику: политика хранения содержимого (отключить логирование содержимого или задать срок хранения) |
| Корпоративный сертификат может быть недоступен | `TLS_MODE=internal`; ключ корня CA Caddy на ВМ — клиенты доверяют корню только на уровне приложения, `caddy-data` в бэкапе как секрет |
| Характеристики ВМ (RAM/диск) неизвестны | Вопрос в заявке DevOps |
| Точный домен и процедура выпуска сертификата | Вопрос в заявке DevOps |

## 14. Источники

- NVIDIA RTX PRO 5000 72GB: https://blogs.nvidia.com/blog/rtx-pro-5000-72gb-blackwell-gpu/ ,
  https://www.servethehome.com/nvidia-rtx-pro-5000-blackwell-with-72gb-out/
- vLLM v0.29.0 (образы, CUDA 13.0): https://github.com/vllm-project/vllm/releases/tag/v0.29.0 ;
  Dockerfile с `TORCH_CUDA_ARCH_LIST` включая 12.0: https://github.com/vllm-project/vllm/blob/main/docker/Dockerfile ;
  поддержка GPU: https://docs.vllm.ai/en/latest/getting_started/installation/gpu.html
- Опыт SM120 (RTX PRO Blackwell): https://github.com/voipmonitor/rtx6kpro/blob/master/inference-engines/vllm.md
- Qwen3.8-27B: https://huggingface.co/Qwen/Qwen3.8-27B , https://huggingface.co/Qwen/Qwen3.8-27B-FP8 ;
  vLLM recipe: https://recipes.vllm.ai/Qwen/Qwen3.8-27B ;
  `reasoning_effort=high` → 500: https://github.com/QwenLM/Qwen3.8/issues/217
- Qwen3.6-27B-FP8 (флаги vLLM): https://huggingface.co/Qwen/Qwen3.6-27B-FP8
- Gemma 4 31B: https://huggingface.co/google/gemma-4-31b-it
- vLLM — один API-ключ: https://github.com/vllm-project/production-stack/issues/759
- Инциденты LiteLLM 2026: https://docs.litellm.ai/blog/security-update-march-2026 ,
  https://docs.litellm.ai/blog/cve-2026-42208-litellm-proxy-sql-injection ,
  https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-teampcp-supply-chain-campaign/
- Bifrost: https://github.com/maximhq/bifrost , https://docs.getbifrost.ai/features/governance/virtual-keys ,
  https://www.getmaxim.ai/bifrost/guides/providers/vllm
- Драйвер 580 в Ubuntu: https://ubuntuhandbook.org/index.php/2025/09/ubuntu-added-nvidia-580-driver/
- Эмбеддинги 2026: https://www.bentoml.com/blog/a-guide-to-open-source-embedding-models
