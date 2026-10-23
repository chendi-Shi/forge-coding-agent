#!/usr/bin/env bash
# Install Docker Engine into Ubuntu WSL using Docker's signed apt repository.
# Reference: https://docs.docker.com/engine/install/ubuntu/
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' 'Run this provisioning script as root in the selected Ubuntu WSL distribution.' >&2
  exit 1
fi
. /etc/os-release
if [ "$ID" != ubuntu ] || [ "${VERSION_CODENAME:-}" != noble ]; then
  printf '%s\n' 'This local provisioning script targets Ubuntu 24.04 (noble).' >&2
  exit 1
fi
if ! command -v dockerd >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y --no-install-recommends ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  if [ ! -f /etc/apt/keyrings/docker.asc ]; then
    curl -fsSL --retry 3 https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
  fi
  if [ ! -f /etc/apt/sources.list.d/docker.sources ]; then
    cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  fi
  apt-get update -qq
  apt-get install -y --no-install-recommends docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
/usr/bin/docker version --format '{{.Server.Version}}'
/usr/bin/docker compose version
