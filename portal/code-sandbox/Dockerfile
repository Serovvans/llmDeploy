# Образ code-sandbox (docs/portal-design.md §3.3, §8.5; docs/portal-api.md §7.5.12, §13.5).
# Цель runtime — ВМ; цель stand — стенд portal/dev: тот же образ плюс заглушка SDK.

# Тот же SDK, что в global.json (без перехода на другую версию): из его targeting pack
# берутся эталонные сборки .NET 8 в /app/ref.
FROM mcr.microsoft.com/dotnet/sdk:8.0.426-bookworm-slim AS build
WORKDIR /src
# Зависимости отдельным слоем: правка кода не перекачивает пакеты.
COPY global.json Directory.Build.props Directory.Build.targets ./
COPY src/CodeSandbox/CodeSandbox.csproj src/CodeSandbox/packages.lock.json src/CodeSandbox/
RUN dotnet restore src/CodeSandbox/CodeSandbox.csproj --locked-mode
COPY src/ src/
RUN dotnet publish src/CodeSandbox/CodeSandbox.csproj -c Release --no-restore -o /app

FROM build AS stub
COPY stub-sdk/ stub-sdk/
RUN dotnet build stub-sdk/eLiteGIS.Server -c Release -o /stub/eLiteGIS.Server \
    && dotnet build stub-sdk/Atemiko.eLiteMap -c Release -o /stub/Atemiko.eLiteMap

# Среда ASP.NET Core той же версии, что в SDK 8.0.426: служба — веб-приложение.
FROM mcr.microsoft.com/dotnet/aspnet:8.0.32-bookworm-slim AS runtime
# /index с владельцем app: новый том индекса наследует права, и разовый запуск index
# пишет в него без root. /sdk и /packages монтируются только для чтения.
RUN install -d -o app -g app /index && install -d /sdk /packages
WORKDIR /app
COPY --from=build /app ./
# Без канала диагностики: к процессу не подключиться отладчиком или профилировщиком,
# и среда не создаёт его сокет в /tmp.
ENV DOTNET_EnableDiagnostics=0
USER $APP_UID
ENTRYPOINT ["/app/code-sandbox"]
CMD ["serve"]

FROM runtime AS stand
COPY --from=stub /stub/eLiteGIS.Server/eLiteGIS.Server.dll /stub/Atemiko.eLiteMap/Atemiko.eLiteMap.dll /stub-sdk/
COPY stub-sdk/plugins.txt /stub-sdk/
