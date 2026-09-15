# LLM-сервис на RTX PRO 5000 72 ГБ

Развёртывание self-hosted мультимодальной LLM (Qwen3.8-27B-FP8 на vLLM) с gateway
Bifrost (API-ключи, лимиты) за Caddy (TLS) на ВМ заказчика. Источник истины по
архитектуре и решениям — `docs/design.md`; сетевые требования — `docs/devops-request.md`.

## Структура

- `deploy/` — docker-compose, Caddyfile, конфиги Bifrost и мониторинга, `.env.example`.
- `scripts/` — uv-проект: preflight, смоук-тест, управление ключами, бенчмарк.
- `docs/` — дизайн, заявка DevOps, runbook.
- `.claude/agents/` — `infra-engineer` (deploy/), `scripts-engineer` (scripts/),
  `reviewer` (только чтение).

## Правила проекта

- Образы и зависимости пинятся по версии; `latest` запрещён.
- Наружу только 443 (Caddy). vLLM не публикует порты; Bifrost — только `127.0.0.1:8080`.
- Секреты только в `.env` (в `.gitignore`); в репозитории — `.env.example` без значений.
- Всё, чего нет в `docs/design.md`, сначала вносится в дизайн, потом в код.
- Локально GPU нет: проверяем `docker compose config`, линтеры и тесты; всё, что требует
  GPU, помечаем как «проверить на ВМ».
