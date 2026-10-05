# LLM-сервис на RTX PRO 5000 72 ГБ

Self-hosted мультимодальная LLM для сотрудников и приложений компании:
vLLM (Qwen3.8-27B-FP8) → Bifrost (API-ключи, лимиты) → Caddy (HTTPS), и портал
сотрудников на той же ВМ: чат, база знаний, SQL-помощник, помощник CoGIS, разбор
документов.

Документы:
- Дизайн и решения: [`docs/design.md`](docs/design.md); портал —
  [`docs/portal-design.md`](docs/portal-design.md), контракт API —
  [`docs/portal-api.md`](docs/portal-api.md), концепция интерфейса —
  [`docs/portal-ui.md`](docs/portal-ui.md)
- Установка и эксплуатация: [`docs/runbook.md`](docs/runbook.md)
- Запуск в VPN до привязки домена, чек-лист проверки на ВМ, открытие доступа из
  интернета: [`docs/vpn-launch.md`](docs/vpn-launch.md)
- Развёртывание с доменом — сертификат Let's Encrypt, порты, проверка снаружи:
  [`docs/domain-setup.md`](docs/domain-setup.md)
- Памятка сотруднику: [`docs/user-guide.md`](docs/user-guide.md)
- Заявка DevOps на сетевые настройки: [`docs/devops-request.md`](docs/devops-request.md)

Структура репозитория:

| Каталог | Что там |
|---|---|
| `deploy/` | `docker-compose.yml`, `.env.example`, Caddyfile, конфиги Bifrost и мониторинга, systemd-юнит |
| `scripts/` | установка по этапам, preflight, сертификат, запуск стека, смоук-тест, ключи, резервная копия, бенчмарк (uv-проект) |
| `portal/backend/` | бэкенд портала: FastAPI, воркер базы знаний, миграции, `config/*.yaml` |
| `portal/frontend/` | интерфейс: React, `@skbkontur/react-ui`; `e2e/` — сквозные тесты Playwright |
| `portal/dev/` | стенд без GPU с заглушкой модели |
| `docs/` | документы из списка выше |
| `.claude/agents/` | агенты разработки; правила и пайплайн — в `CLAUDE.md` |

Развёртывание идёт этапами (`design.md` §3.7, §3.8): модель можно поднять и проверить
сразу; внешний доступ добавляется, когда есть имя (или IP ВМ) и сертификат; портал —
третьим этапом поверх него.

Установка на ВМ (подробно — runbook §2):

```bash
make host                 # драйвер, Docker, NVIDIA Container Toolkit, uv; затем sudo reboot
make preflight            # готовность ВМ: GPU, драйвер, сеть, диск
make model                # этап 1: vLLM на 127.0.0.1:8000
make smoke-model          # проверка модели: текст, зрение, json_schema, tool call
make bench                # TTFT и throughput

make cert                 # сертификат Let's Encrypt для LLM_HOSTNAME из deploy/.env (docs/domain-setup.md)
make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp PROFILES=embeddings   # этап 2: Caddy + Bifrost (или TLS_MODE=internal)
make key NAME=app-crm     # ключ клиента

make key NAME=portal      # этап 3: ключ портала — вписать в PORTAL_LLM_API_KEY в deploy/.env
make portal               # портал сотрудников
make portal-admin LOGIN=<логин> NAME="<ФИО>"   # первый администратор
make backup DIR=<каталог вне репозитория>      # резервная копия
```

`make help` — полный список команд. Локальные проверки без GPU — `make check`; стенд
портала и сквозные тесты — `make dev-up`, `make dev-reset`, `make dev-down`,
`make dev-test-install`, `make dev-test` (runbook §6).

Адрес сервиса из интернета — `https://llm.<домен>:18443` (проброс
`195.122.229.203:18443 → 192.168.25.8:8080`):
- сотрудникам — портал: `https://llm.<домен>:18443`;
- клиентам API — `base_url=https://llm.<домен>:18443/v1`, `api_key=sk-bf-...`,
  `model="default"`.
