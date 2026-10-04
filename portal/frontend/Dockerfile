# Образ portal-web: собранная статика SPA на Caddy (docs/portal-design.md §3).

FROM node:24.21.0-bookworm-slim AS build
WORKDIR /app
# Зависимости отдельным слоем: правка кода не переустанавливает их.
COPY package.json package-lock.json .npmrc ./
RUN npm ci
COPY index.html tsconfig.json vite.config.ts ./
COPY public/ public/
COPY src/ src/
RUN npm run build

FROM caddy:2.11.4
# Правила выдачи — docs/portal-api.md §1.1. Конфигурация записана здесь, а не отдельным
# файлом: она часть образа и вне его не используется.
COPY <<'EOF' /etc/caddy/Caddyfile
{
	# Сервис стоит за Caddy точки входа: TLS и admin API ему не нужны, а состояние на
	# диск он не пишет.
	admin off
	auto_https off
	persist_config off
}

:8080 {
	root * /srv

	# Имена файлов в /assets содержат хеш содержимого — можно кэшировать навсегда.
	@asset {
		path /assets/*
		file
	}
	handle @asset {
		header Cache-Control "public, max-age=31536000, immutable"
		file_server
	}

	# Остальное отдаётся с проверкой свежести: у index.html и theme-init.js хеша в
	# имени нет, после обновления портала браузер должен получить новые.
	header Cache-Control no-cache

	@static {
		path /theme-init.js /fonts/*
		file
	}
	handle @static {
		file_server
	}

	# Адреса экранов: маршрут выбирает само приложение.
	@screen path / /login /login/code /password /second-factor /chat /chat/* /knowledge /sql /sql/* /cogis /cogis/* /documents /documents/* /admin/users /profile
	handle @screen {
		rewrite * /index.html
		file_server
	}

	# Всё прочее (в том числе /metrics, /docs, /healthz) — 404; телом идёт приложение,
	# чтобы интерфейс показал «Страница не найдена».
	handle {
		rewrite * /index.html
		file_server {
			status 404
		}
	}
}
EOF
COPY --from=build /app/dist /srv
# Caddy создаёт служебные каталоги в /data и /config даже без сертификатов.
RUN chown -R 10001:10001 /data /config
USER 10001:10001
