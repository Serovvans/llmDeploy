# LLM-сервис на RTX PRO 5000 72 ГБ

Self-hosted мультимодальная LLM для сотрудников и приложений компании:
vLLM (Qwen3.8-27B-FP8) → Bifrost (API-ключи, лимиты) → Caddy (HTTPS).

- Дизайн и решения: [`docs/design.md`](docs/design.md)
- Заявка DevOps на сетевые настройки: [`docs/devops-request.md`](docs/devops-request.md)
- Установка и эксплуатация: [`docs/runbook.md`](docs/runbook.md)

Развёртывание идёт двумя независимыми этапами (`design.md` §3.7): модель можно
поднять и проверить сразу, внешний доступ добавляется, когда DevOps выдадут DNS-имя,
сертификат и правило на 443.

Установка на ВМ (подробно — runbook §2):

```bash
make host                 # драйвер, Docker, NVIDIA Container Toolkit, uv; затем sudo reboot
make preflight            # готовность ВМ: GPU, драйвер, сеть, диск
make model                # этап 1: vLLM на 127.0.0.1:8000
make smoke-model          # проверка модели: текст, зрение, json_schema, tool call
make bench                # TTFT и throughput

make gateway LLM_HOSTNAME=llm.<домен> TLS_MODE=corp   # этап 2: Caddy + Bifrost (или TLS_MODE=internal)
make key NAME=app-crm     # ключ клиента
```

`make help` — полный список команд.

Клиентам: `base_url=https://llm.<домен>/v1`, `api_key=sk-bf-...`, `model="default"`.
