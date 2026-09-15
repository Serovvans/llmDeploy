# LLM-сервис на RTX PRO 5000 72 ГБ

Self-hosted мультимодальная LLM для сотрудников и приложений компании:
vLLM (Qwen3.8-27B-FP8) → Bifrost (API-ключи, лимиты) → Caddy (HTTPS).

- Дизайн и решения: [`docs/design.md`](docs/design.md)
- Заявка DevOps на сетевые настройки: [`docs/devops-request.md`](docs/devops-request.md)
- Установка и эксплуатация: [`docs/runbook.md`](docs/runbook.md)

Установка на ВМ (подробно — runbook §2):

```bash
sudo bash scripts/install_host.sh     # драйвер, Docker, NVIDIA Container Toolkit, uv; затем sudo reboot
sudo bash scripts/preflight.sh
sudo bash scripts/install.sh --hostname llm.<домен> --tls corp      # или --tls internal без корп. сертификата
```

Клиентам: `base_url=https://llm.<домен>/v1`, `api_key=sk-bf-...`, `model="default"`.
