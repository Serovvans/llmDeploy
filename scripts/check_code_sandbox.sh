#!/usr/bin/env bash
# Сборка и тесты песочницы сборки C# (portal/code-sandbox) для make check.
#
# Запуск на машине разработчика, без sudo:
#   bash scripts/check_code_sandbox.sh
#
# .NET SDK на машине не нужен: проверки идут в закреплённом контейнере SDK — той же
# версии, что в portal/code-sandbox/global.json и в Dockerfile песочницы. Исходники
# монтируются только для чтения и копируются в контейнер: bin/ и obj/ в дереве не
# появляются. Пакеты NuGet кешируются в томе llm-dev-nuget; первому запуску нужна сеть
# (mcr.microsoft.com, api.nuget.org). Тестам нужны шаблоны мастера из
# portal/backend/config/code_templates — они монтируются рядом.
# Если демон Docker недоступен, проверки пропускаются с предупреждением.
#
# Проверки: dotnet restore --locked-mode (lock-файлы совпадают с проектами), сборка с
# предупреждениями как ошибками, dotnet format --verify-no-changes, dotnet test.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
REPO_DIR="$(dirname "$SCRIPT_DIR")"
readonly REPO_DIR
readonly SANDBOX_DIR="portal/code-sandbox"
readonly TEMPLATES_DIR="portal/backend/config/code_templates"
readonly SDK_IMAGE="mcr.microsoft.com/dotnet/sdk:8.0.426-bookworm-slim"
readonly NUGET_VOLUME="llm-dev-nuget"
readonly SOLUTION="CodeSandbox.sln"

log() {
  echo "==> $1"
}

if ! docker info >/dev/null 2>&1; then
  echo "ВНИМАНИЕ: демон Docker недоступен — сборка и тесты ${SANDBOX_DIR} ПРОПУЩЕНЫ; запустить Docker и повторить make check" >&2
  exit 0
fi

log "${SANDBOX_DIR}: restore, build, format, test в ${SDK_IMAGE}"
docker run --rm \
  -v "${REPO_DIR}/${SANDBOX_DIR}:/src/${SANDBOX_DIR}:ro" \
  -v "${REPO_DIR}/${TEMPLATES_DIR}:/src/${TEMPLATES_DIR}:ro" \
  -v "${NUGET_VOLUME}:/nuget" -e NUGET_PACKAGES=/nuget \
  -e DOTNET_CLI_TELEMETRY_OPTOUT=1 -e DOTNET_NOLOGO=1 \
  "$SDK_IMAGE" bash -euo pipefail -c "
    cp -r /src /work
    # Результаты прежних сборок на машине разработчика: сборка — с чистого дерева.
    find /work -type d \( -name bin -o -name obj -o -name TestResults \) -prune -exec rm -rf {} +
    cd /work/${SANDBOX_DIR}
    dotnet restore ${SOLUTION} --locked-mode
    dotnet build ${SOLUTION} -c Release --no-restore -warnaserror
    dotnet format ${SOLUTION} --verify-no-changes --no-restore
    dotnet test ${SOLUTION} -c Release --no-build
  "
