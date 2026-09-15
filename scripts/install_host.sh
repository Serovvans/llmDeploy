#!/usr/bin/env bash
# Подготовка ВМ к установке сервиса (docs/design.md §10): драйвер NVIDIA, Docker Engine,
# NVIDIA Container Toolkit, uv, синхронизация времени.
#
# Запуск на ВМ с Ubuntu 22.04/24.04:
#   sudo bash scripts/install_host.sh
#
# Идемпотентен: уже установленные компоненты пропускаются, повторный запуск безопасен.
# Чужой драйвер NVIDIA или пакет docker.io не удаляет — останавливается и говорит, что
# сделать. После установки драйвера нужна перезагрузка; затем — sudo scripts/preflight.sh.
set -euo pipefail

readonly DRIVER_BRANCH=580
readonly DRIVER_PACKAGE="nvidia-driver-${DRIVER_BRANCH}-server-open"
readonly MIN_DOCKER_MAJOR=27
readonly UV_VERSION="0.12.15"
readonly DOCKER_KEYRING="/etc/apt/keyrings/docker.asc"
readonly NVIDIA_KEYRING="/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg"
readonly NVIDIA_LIST_URL="https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list"

reboot_required=0

log() {
  echo "==> $1"
}

die() {
  echo "ОШИБКА: $1" >&2
  echo "что делать: $2" >&2
  exit 1
}

apt_install() {
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

is_installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

check_system() {
  [[ "$(uname -s)" == "Linux" ]] || die "скрипт запускается на ВМ с Ubuntu" "запустить на ВМ"
  [[ "$EUID" -eq 0 ]] || die "нужны права root" "sudo bash scripts/install_host.sh"
  # shellcheck source=/dev/null
  source /etc/os-release
  [[ "${ID:-}" == "ubuntu" && ("${VERSION_ID:-}" == "22.04" || "${VERSION_ID:-}" == "24.04") ]] ||
    die "поддерживаются Ubuntu 22.04 и 24.04, найдено ${PRETTY_NAME:-неизвестно}" \
      "переустановить ВМ на Ubuntu 24.04 LTS (docs/design.md §10)"
}

install_base_packages() {
  log "базовые пакеты"
  apt-get update
  apt_install ca-certificates curl gnupg openssl
}

install_driver() {
  if is_installed "$DRIVER_PACKAGE"; then
    if grep -q "Open Kernel Module" /proc/driver/nvidia/version 2>/dev/null; then
      log "драйвер ${DRIVER_PACKAGE} установлен и загружен — пропуск"
    else
      log "драйвер ${DRIVER_PACKAGE} установлен, но модуль не загружен — нужна перезагрузка"
      reboot_required=1
    fi
    return
  fi

  local other
  other=$(dpkg-query -W -f='${Package} ${Status}\n' 'nvidia-driver-*' 2>/dev/null |
    awk '/install ok installed/ { print $1 }' | paste -sd' ' -) || true
  [[ -z "$other" ]] ||
    die "уже установлен другой драйвер NVIDIA: ${other}; Blackwell требует ветку ${DRIVER_BRANCH} с open kernel modules" \
      "sudo apt purge 'nvidia-driver-*' 'libnvidia-*' && sudo apt autoremove, затем повторить скрипт"

  log "драйвер ${DRIVER_PACKAGE}"
  apt_install "$DRIVER_PACKAGE"
  reboot_required=1
}

docker_major() {
  docker version --format '{{.Client.Version}}' 2>/dev/null | cut -d. -f1
}

install_docker() {
  if command -v docker >/dev/null 2>&1 && is_installed docker-ce; then
    local major
    major=$(docker_major)
    [[ "$major" =~ ^[0-9]+$ && "$major" -ge "$MIN_DOCKER_MAJOR" ]] ||
      die "Docker Engine ${major:-?}; нужен >= ${MIN_DOCKER_MAJOR}" \
        "sudo apt-get update && sudo apt-get install --only-upgrade docker-ce docker-ce-cli docker-compose-plugin"
    log "Docker Engine уже установлен — пропуск"
    return
  fi

  local conflicting=()
  local pkg
  for pkg in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
    if is_installed "$pkg"; then conflicting+=("$pkg"); fi
  done
  [[ "${#conflicting[@]}" -eq 0 ]] ||
    die "установлены пакеты, конфликтующие с Docker Engine: ${conflicting[*]}" \
      "sudo apt remove ${conflicting[*]} (контейнеры и образы этих пакетов будут недоступны), затем повторить скрипт"

  log "Docker Engine из download.docker.com"
  install -m 0755 -d "$(dirname "$DOCKER_KEYRING")"
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o "$DOCKER_KEYRING"
  chmod a+r "$DOCKER_KEYRING"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=${DOCKER_KEYRING}] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update
  apt_install docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker

  local major
  major=$(docker_major)
  [[ "$major" =~ ^[0-9]+$ && "$major" -ge "$MIN_DOCKER_MAJOR" ]] ||
    die "установился Docker Engine ${major:-?}; нужен >= ${MIN_DOCKER_MAJOR}" \
      "проверить репозиторий /etc/apt/sources.list.d/docker.list и apt-cache policy docker-ce"
}

install_container_toolkit() {
  if is_installed nvidia-container-toolkit && grep -q '"nvidia"' /etc/docker/daemon.json 2>/dev/null; then
    log "NVIDIA Container Toolkit уже установлен и подключён к Docker — пропуск"
    return
  fi

  log "NVIDIA Container Toolkit из nvidia.github.io"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o "$NVIDIA_KEYRING"
  curl -fsSL "$NVIDIA_LIST_URL" |
    sed "s#deb https://#deb [signed-by=${NVIDIA_KEYRING}] https://#g" \
      >/etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update
  apt_install nvidia-container-toolkit
  # Регистрирует runtime nvidia в /etc/docker/daemon.json — без него --gpus и
  # deploy.resources.devices в compose не видят GPU.
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
}

install_uv() {
  if command -v uv >/dev/null 2>&1 && [[ "$(uv --version)" == "uv ${UV_VERSION}"* ]]; then
    log "uv ${UV_VERSION} уже установлен — пропуск"
    return
  fi

  log "uv ${UV_VERSION} в /usr/local/bin"
  # /usr/local/bin есть в PATH и у пользователей, и у sudo (secure_path).
  curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" |
    env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
}

enable_ntp() {
  log "синхронизация времени"
  timedatectl set-ntp true
}

main() {
  check_system
  install_base_packages
  install_driver
  install_docker
  install_container_toolkit
  install_uv
  enable_ntp

  echo
  if [[ "$reboot_required" -eq 1 ]]; then
    echo "ГОТОВО, НУЖНА ПЕРЕЗАГРУЗКА: драйвер NVIDIA загрузится после sudo reboot."
    echo "После перезагрузки: sudo bash scripts/preflight.sh"
  else
    echo "ГОТОВО. Следующий шаг: sudo bash scripts/preflight.sh"
  fi
}

main "$@"
