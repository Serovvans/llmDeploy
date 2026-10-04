# Привязка поддомена и внешний доступ

Краткая инструкция для этапа 2 (`docs/design.md` §3.7) при доступе через публичный
поддомен заказчика (домен на Timeweb). Выполнять после того, как сисадмин перенаправит
проброс на порт 443 ВМ.

> Расхождение с дизайном: `docs/design.md` описывает доступ только через VPN и внутреннее
> DNS-имя, а Let's Encrypt числится в отвергнутых вариантах (§3.4). Публичный доступ нужно
> подтвердить у заказчика и внести в дизайн.

## Исходные данные

| Что | Значение |
|---|---|
| Внешний IP | `195.122.229.203` |
| ВМ | `192.168.25.8`, репозиторий в `/opt/llm` |
| Поддомен | `llm.<домен>` (ниже — `$HOST`) |
| Внешний порт | `443` или `18443` (ниже — `$PORT`) |

Требуемый проброс: `195.122.229.203:$PORT → 192.168.25.8:443` (TCP). Цель — именно 443
(Caddy); на 8080 слушает админка Bifrost, она привязана к `127.0.0.1` и наружу не
публикуется.

От внешнего порта зависит только URL клиентов: при `443` — `https://$HOST/v1`, при
`18443` — `https://$HOST:18443/v1`. Остальные шаги одинаковы.

## 1. Пароль root

```bash
openssl rand -base64 24        # сгенерировать, скопировать
sudo passwd root
```

Текущую SSH-сессию не закрывать, пока вход не проверен во второй. Пароль передать
сисадмину отдельным каналом от адреса ВМ (менеджер паролей, одноразовая ссылка).

## 2. DNS-запись

Панель Timeweb → Домены → домен → DNS → добавить запись: тип `A`, поддомен `llm`,
значение `195.122.229.203`.

```bash
dig +short $HOST               # 195.122.229.203
```

## 3. Сертификат Let's Encrypt (DNS-01)

Проверка через DNS не зависит от внешнего порта. Условия: зона домена обслуживается
NS-серверами Timeweb Cloud, в панели выпущен API-токен.

```bash
sudo install -d -m 700 /opt/llm/lego
sudo sh -c 'umask 077; cat > /opt/llm/lego/timeweb.env' <<'EOF'
TIMEWEBCLOUD_AUTH_TOKEN=<токен>
EOF

sudo docker run --rm --env-file /opt/llm/lego/timeweb.env \
  -v /opt/llm/lego:/lego goacme/lego:<версия> \
  --path /lego --accept-tos --email <e-mail> \
  --dns timewebcloud --domains $HOST run

sudo install -d -m 700 /opt/llm/deploy/certs
sudo install -m 644 /opt/llm/lego/certificates/$HOST.crt /opt/llm/deploy/certs/fullchain.pem
sudo install -m 600 /opt/llm/lego/certificates/$HOST.key /opt/llm/deploy/certs/privkey.pem
```

Перед запуском сверить с документацией lego имя провайдера (`timewebcloud`), переменную
токена и актуальный тег образа (`latest` запрещён) — они не проверены на ВМ.

Каталог `/opt/llm/lego` содержит токен и ключ учётной записи ACME: в репозиторий не
попадает, хранить как секрет.

## 4. Запуск gateway

```bash
cd /opt/llm
make gateway LLM_HOSTNAME=$HOST TLS_MODE=corp
make ps
sudo ss -ltnp | grep -E ':443|:8080'   # 443 — 0.0.0.0, 8080 — только 127.0.0.1
sudo ufw status                        # если active: sudo ufw allow 443/tcp
```

## 5. Проверка снаружи

Не из сети заказчика (обращение на собственный внешний IP изнутри может не работать).
При `$PORT=443` порт из URL убрать.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://$HOST:$PORT/v1/models   # 401 или 403
curl -sS -o /dev/null -w '%{http_code}\n' https://$HOST:$PORT/            # 404
```

## 6. Ключ и смоук-тест

```bash
make key NAME=smoke                    # на ВМ

# с рабочей станции, из scripts/
LLM_BASE_URL=https://$HOST:$PORT/v1 LLM_API_KEY=sk-bf-... uv run smoke_test.py

make keys && make revoke ID=<id>       # на ВМ: отозвать тестовый ключ
```

Клиентам: `base_url="https://$HOST:$PORT/v1"`, ключи — `make key NAME=<приложение>`.

## 7. Продление сертификата

Сертификат действует 90 дней и сам не продлевается. Раз в ~60 дней (cron или
systemd-таймер):

```bash
sudo docker run --rm --env-file /opt/llm/lego/timeweb.env \
  -v /opt/llm/lego:/lego goacme/lego:<версия> \
  --path /lego --email <e-mail> \
  --dns timewebcloud --domains $HOST renew --days 30
```

Затем снова разложить файлы (команды `install` из шага 3) и перезапустить Caddy:

```bash
sudo docker compose -f /opt/llm/deploy/docker-compose.yml restart caddy
```

При внешнем порте 443 продление можно отдать самому Caddy (TLS-ALPN-01, без lego и
токена) — для этого нужен отдельный режим `TLS_MODE`, которого в проекте пока нет.
