#!/usr/bin/env bash
# Проверка готовности ВМ к установке по чек-листу docs/design.md §10.
#
# Запуск на ВМ (пользователь в группе docker, иначе через sudo):
#   bash scripts/preflight.sh
#   bash scripts/preflight.sh --release
#       Перед релизом на работающей ВМ (docs/portal-design.md §3.4, §8.2): только Docker,
#       свободное место по бюджету релиза, доступ к Hugging Face и реестрам образов.
#       Карта и память заняты работающим сервисом — их проверяет первая установка.
#
# Проверяет: GPU и видеопамять, драйвер >= 580 с open kernel modules, Docker >= 27,
# NVIDIA Container Toolkit (тестовый контейнер с nvidia-smi), свободное место в каталоге
# docker, RAM, vCPU, загрузку файлов модели с Hugging Face (config.json и первый КиБ весов —
# веса идут через Xet CDN *.hf.co), доступ к реестрам образов и их CDN, NTP, наличие uv.
# Выполняет все проверки, печатает сводку PASS/FAIL; код выхода 1, если что-то не прошло.
#
# Окружение (если не задано — берётся из deploy/.env):
#   MODEL_ID  HF-идентификатор модели; по умолчанию Qwen/Qwen3.8-27B-FP8
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
readonly ENV_FILE="${SCRIPT_DIR}/../deploy/.env"

readonly MIN_DRIVER_MAJOR=580
# Карта 72 ГБ; nvidia-smi показывает немного меньше номинала.
readonly MIN_GPU_MEMORY_MIB=70000
readonly MIN_DOCKER_MAJOR=27
readonly MIN_FREE_DISK_GB=300
# Бюджет релиза портала (docs/portal-design.md §3.4): веса, образы и кеш сборки — 15 ГБ;
# квота на медиа — 50 ГБ; тома портала с ростом — 10 ГБ; восстановление из копии
# разворачивает медиа и тома ещё раз. Квота и размер томов — допущения дизайна: при
# других значениях поменять их здесь.
readonly RELEASE_IMAGES_GB=15
readonly RELEASE_MEDIA_QUOTA_GB=50
readonly RELEASE_VOLUMES_GB=10
readonly MIN_FREE_RELEASE_GB=$((RELEASE_IMAGES_GB + 2 * (RELEASE_MEDIA_QUOTA_GB + RELEASE_VOLUMES_GB)))
# 64 ГБ по заявке; MemTotal меньше номинала на резерв ядра и прошивки.
readonly MIN_RAM_GIB=62
readonly MIN_VCPU=8
readonly CUDA_TEST_IMAGE="nvidia/cuda:13.0.1-base-ubuntu24.04"
readonly DEFAULT_MODEL_ID="Qwen/Qwen3.8-27B-FP8"
readonly NET_TIMEOUT_S=10
# Загрузка с Hugging Face идёт через редиректы — даём больше времени.
readonly HF_TIMEOUT_S=30
readonly ENDPOINTS=(
  "https://registry-1.docker.io/v2/"
  "https://production.cloudflare.docker.com/"
  "https://ghcr.io/v2/"
  "https://pkg-containers.githubusercontent.com/"
  "https://nvcr.io/v2/"
  # Образы .NET песочницы сборки C# и пакеты NuGet: при сборке образа и загрузке
  # офлайн-набора (docs/portal-design.md §7).
  "https://mcr.microsoft.com/v2/"
  "https://api.nuget.org/v3/index.json"
)

declare -a SUMMARY=()
failures=0
docker_ok=0

pass() {
  echo "PASS  $1"
  SUMMARY+=("PASS  $1")
}

# fail "что не прошло" "что сделать"
fail() {
  echo "FAIL  $1"
  echo "      что делать: $2"
  SUMMARY+=("FAIL  $1" "      что делать: $2")
  failures=$((failures + 1))
}

# Значение переменной из окружения, иначе последняя строка NAME=... из deploy/.env.
env_value() {
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
  elif [[ -f "$ENV_FILE" ]]; then
    sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1 | sed -e "s/^[\"']//" -e "s/[\"']\$//"
  fi
}

check_gpu() {
  if ! command -v nvidia-smi >/dev/null 2>&1; then
    fail "GPU: nvidia-smi не найден (драйвер NVIDIA не установлен)" \
      "sudo apt install nvidia-driver-${MIN_DRIVER_MAJOR}-server-open && sudo reboot"
    return
  fi

  local output
  if ! output=$(nvidia-smi --query-gpu=name,memory.total,driver_version \
    --format=csv,noheader,nounits 2>&1); then
    fail "GPU: nvidia-smi завершился с ошибкой: ${output}" \
      "проверить проброс GPU в ВМ и загрузку модуля: sudo dmesg | grep -i nvidia"
    return
  fi

  local name memory driver
  IFS=',' read -r name memory driver <<<"$(head -n 1 <<<"$output")"
  memory="${memory// /}"
  driver="${driver// /}"

  if [[ "$name" == *"RTX PRO 5000"* && "$memory" =~ ^[0-9]+$ && "$memory" -ge "$MIN_GPU_MEMORY_MIB" ]]; then
    pass "GPU: ${name}, ${memory} МиБ"
  else
    fail "GPU: найдено «${name}», ${memory} МиБ; ожидается RTX PRO 5000 с >= ${MIN_GPU_MEMORY_MIB} МиБ" \
      "проверить, что в ВМ проброшена нужная карта целиком (passthrough, без vGPU-профиля)"
  fi

  if [[ "${driver%%.*}" =~ ^[0-9]+$ && "${driver%%.*}" -ge "$MIN_DRIVER_MAJOR" ]]; then
    pass "драйвер NVIDIA ${driver}"
  else
    fail "драйвер NVIDIA ${driver}; нужен >= ${MIN_DRIVER_MAJOR}" \
      "sudo apt install nvidia-driver-${MIN_DRIVER_MAJOR}-server-open && sudo reboot"
  fi
}

check_open_kernel_modules() {
  if grep -q "Open Kernel Module" /proc/driver/nvidia/version 2>/dev/null; then
    pass "драйвер NVIDIA: open kernel modules"
  else
    fail "драйвер NVIDIA: не open kernel modules (или модуль не загружен); Blackwell требует -open" \
      "sudo apt purge 'nvidia-driver-*' && sudo apt install nvidia-driver-${MIN_DRIVER_MAJOR}-server-open && sudo reboot"
  fi
}

check_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    fail "Docker: не установлен" \
      "установить Docker Engine >= ${MIN_DOCKER_MAJOR} по https://docs.docker.com/engine/install/ubuntu/"
    return
  fi

  local version
  if ! version=$(docker version --format '{{.Server.Version}}' 2>&1); then
    fail "Docker: нет доступа к демону: ${version}" \
      "sudo systemctl enable --now docker; добавить пользователя в группу: sudo usermod -aG docker \$USER и перелогиниться"
    return
  fi

  if [[ "${version%%.*}" =~ ^[0-9]+$ && "${version%%.*}" -ge "$MIN_DOCKER_MAJOR" ]]; then
    pass "Docker Engine ${version}"
    docker_ok=1
  else
    fail "Docker Engine ${version}; нужен >= ${MIN_DOCKER_MAJOR}" \
      "обновить Docker Engine из репозитория download.docker.com"
  fi
}

check_container_toolkit() {
  if [[ "$docker_ok" -ne 1 ]]; then
    fail "NVIDIA Container Toolkit: не проверен, т. к. не прошла проверка Docker" "сначала исправить Docker"
    return
  fi

  echo "....  запуск ${CUDA_TEST_IMAGE} с --gpus all (при первом запуске скачивается образ)"
  local output
  if output=$(docker run --rm --gpus all "$CUDA_TEST_IMAGE" nvidia-smi -L 2>&1); then
    pass "NVIDIA Container Toolkit: контейнер видит GPU (${output%%$'\n'*})"
  else
    fail "NVIDIA Container Toolkit: контейнер с --gpus all не запустился: ${output##*$'\n'}" \
      "sudo apt install nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker"
  fi
}

# check_disk МИНИМУМ_ГБ: свободное место в каталоге данных Docker (образы и тома).
check_disk() {
  local min_gb="$1" dir="/var/lib/docker"
  if [[ "$docker_ok" -eq 1 ]]; then
    dir=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || dir="/var/lib/docker"
  fi
  # Каталог может ещё не существовать — смотрим ближайший существующий родитель.
  while [[ ! -d "$dir" ]]; do
    dir=$(dirname "$dir")
  done

  local free_bytes free_gb
  free_bytes=$(df -P -B1 "$dir" | awk 'NR == 2 { print $4 }')
  free_gb=$((free_bytes / 1000000000))
  if [[ "$free_gb" -ge "$min_gb" ]]; then
    pass "диск: свободно ${free_gb} ГБ в ${dir} (нужно >= ${min_gb} ГБ)"
  else
    fail "диск: свободно ${free_gb} ГБ в ${dir}; нужно >= ${min_gb} ГБ" \
      "расширить диск ВМ или перенести data-root Docker на отдельный том (/etc/docker/daemon.json)"
  fi
}

check_ram() {
  local total_kib total_gib
  total_kib=$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo)
  total_gib=$((total_kib / 1024 / 1024))
  if [[ "$total_gib" -ge "$MIN_RAM_GIB" ]]; then
    pass "RAM: ${total_gib} ГиБ"
  else
    fail "RAM: ${total_gib} ГиБ; нужно >= 64 ГБ (загрузка весов идёт через RAM)" \
      "увеличить память ВМ (вопрос 1 в docs/devops-request.md)"
  fi
}

check_cpu() {
  local vcpu
  vcpu=$(nproc)
  if [[ "$vcpu" -ge "$MIN_VCPU" ]]; then
    pass "vCPU: ${vcpu}"
  else
    fail "vCPU: ${vcpu}; нужно >= ${MIN_VCPU}" "увеличить число vCPU ВМ (вопрос 1 в docs/devops-request.md)"
  fi
}

check_network() {
  if ! command -v curl >/dev/null 2>&1; then
    fail "сеть: curl не найден, доступ к реестрам не проверен" "sudo apt install curl"
    return
  fi

  local url host code rc
  for url in "${ENDPOINTS[@]}"; do
    host="${url#https://}"
    host="${host%%/*}"
    rc=0
    # Любой HTTP-ответ (в т. ч. 401/404) означает, что TCP и TLS до хоста проходят.
    code=$(curl -sS -o /dev/null --max-time "$NET_TIMEOUT_S" -w '%{http_code}' "$url" 2>/dev/null) || rc=$?
    if [[ "$rc" -eq 0 ]]; then
      pass "сеть: ${host} доступен (HTTP ${code})"
    else
      fail "сеть: ${host} недоступен (curl exit ${rc})" \
        "открыть исходящий 443 к ${host} (docs/devops-request.md §1.4); при корпоративном прокси задать HTTPS_PROXY"
    fi
  done
}

# hf_fetch "что качаем" URL [доп. аргументы curl]; код 0 — получен HTTP 200/206.
hf_fetch() {
  local label="$1" url="$2"
  shift 2
  local code rc=0
  code=$(curl -sS -L -o /dev/null --max-time "$HF_TIMEOUT_S" -w '%{http_code}' "$@" "$url" 2>/dev/null) || rc=$?
  if [[ "$rc" -ne 0 ]]; then
    fail "Hugging Face: ${label} не скачивается (curl exit ${rc})" \
      "открыть исходящий 443 к huggingface.co и *.hf.co — веса идут через Xet CDN (docs/devops-request.md §1.4); при корпоративном прокси задать HTTPS_PROXY"
    return 1
  fi
  if [[ "$code" == "200" || "$code" == "206" ]]; then
    pass "Hugging Face: ${label} (HTTP ${code})"
    return 0
  fi
  fail "Hugging Face: ${label} — HTTP ${code}" \
    "проверить MODEL_ID в deploy/.env: HF отвечает 401 и на несуществующую, и на закрытую модель; 404 — нет файла"
  return 1
}

check_huggingface() {
  if ! command -v curl >/dev/null 2>&1; then
    fail "Hugging Face: curl не найден, загрузка модели не проверена" "sudo apt install curl"
    return
  fi

  local model_id base shard
  model_id="$(env_value MODEL_ID)"
  model_id="${model_id:-$DEFAULT_MODEL_ID}"
  base="https://huggingface.co/${model_id}/resolve/main"

  # config.json отдаёт сам huggingface.co; веса — через редирект на Xet CDN (*.hf.co),
  # поэтому отдельно качаем первый КиБ первого файла весов.
  hf_fetch "config.json модели ${model_id}" "${base}/config.json" || return 0
  shard=$(curl -sS -L --max-time "$HF_TIMEOUT_S" "${base}/model.safetensors.index.json" 2>/dev/null |
    grep -o '"[^"]*\.safetensors"' | head -n 1 | tr -d '"') || true
  hf_fetch "первый КиБ весов ${shard:-model.safetensors} (Xet CDN)" \
    "${base}/${shard:-model.safetensors}" --range 0-1023 || true
}

check_uv() {
  if command -v uv >/dev/null 2>&1; then
    pass "uv: $(uv --version)"
  else
    fail "uv: не установлен (нужен для scripts/smoke_test.py и scripts/keys.py)" \
      "установить по https://docs.astral.sh/uv/getting-started/installation/; при запуске через sudo uv должен быть в PATH root"
  fi
}

check_ntp() {
  local synced
  if ! synced=$(timedatectl show --property=NTPSynchronized --value 2>&1); then
    fail "NTP: timedatectl недоступен: ${synced}" "проверить systemd-timesyncd или chrony вручную"
    return
  fi
  if [[ "$synced" == "yes" ]]; then
    pass "NTP: время синхронизировано"
  else
    fail "NTP: время не синхронизировано" \
      "sudo timedatectl set-ntp true; проверить timedatectl timesync-status и доступ к NTP-серверу (UDP 123)"
  fi
}

main() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "preflight.sh запускается на ВМ с Ubuntu, а не на $(uname -s)" >&2
    exit 1
  fi

  case "${1:-}" in
    "")
      check_gpu
      check_open_kernel_modules
      check_docker
      check_container_toolkit
      check_disk "$MIN_FREE_DISK_GB"
      check_ram
      check_cpu
      check_huggingface
      check_network
      check_ntp
      check_uv
      ;;
    --release)
      check_docker
      check_disk "$MIN_FREE_RELEASE_GB"
      check_huggingface
      check_network
      ;;
    *)
      echo "неизвестный аргумент «$1»: ожидается --release или запуск без аргументов" >&2
      exit 1
      ;;
  esac

  echo
  echo "===== Сводка ====="
  printf '%s\n' "${SUMMARY[@]}"
  if [[ "$failures" -eq 0 ]]; then
    echo "ИТОГ: PASS"
  else
    echo "ИТОГ: FAIL (не прошло проверок: ${failures})"
    exit 1
  fi
}

main "$@"
