#!/usr/bin/env bash
# Install the tools the worker's checks and the build sandbox need on CT 110:
#
#   ./install-tools.sh
#
# - golang-go: gofmt and go vet for Go repos (plans/10 P1.5)
# - nodejs, npm, node-corepack: tsc after installing a TypeScript repo's packages (pnpm comes
#   through corepack, pinned by the repo's packageManager field)
# - bubblewrap, socat: Claude Code's Bash sandbox on Linux, and the worker's own jail for
#   dependency installs and checks (plans/10 P1.11)
# - python3-poetry, plus uv-managed Pythons 3.12 and 3.13 in /opt/uv-python: CT 110's Python is
#   3.14, which some locked dependencies have no wheels for yet (asyncpg 0.30)
#
# ruff is already in /opt/ruff. Same SSH path as deploy.sh; safe to run again.
set -euo pipefail

PROXMOX="root@192.168.50.111"
SSH_KEY="$HOME/.ssh/proxmox_media"
VMID=110
PACKAGES="golang-go nodejs npm node-corepack bubblewrap socat python3-poetry"
UV_VERSION="0.13.6"
UV_SHA256="f29c3e94ffabd7e6f3ba521dba169a9b7ec12100bf3257087f0f24f6123f3645"

ssh -i "$SSH_KEY" -o BatchMode=yes "$PROXMOX" "pct exec $VMID -- sh -c 'set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends $PACKAGES >/dev/null
if [ \"\$(/usr/local/bin/uv --version 2>/dev/null | cut -d\" \" -f2)\" != \"$UV_VERSION\" ]; then
  cd /tmp
  curl -sSfLo uv.tgz https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz
  echo \"$UV_SHA256  uv.tgz\" | sha256sum -c - >/dev/null
  tar -xzf uv.tgz
  install -m 755 uv-x86_64-unknown-linux-gnu/uv /usr/local/bin/uv
  rm -rf uv.tgz uv-x86_64-unknown-linux-gnu
fi
UV_PYTHON_INSTALL_DIR=/opt/uv-python /usr/local/bin/uv python install --quiet 3.12 3.13
chmod -R a+rX /opt/uv-python
go version
echo node \$(node --version), npm \$(npm --version), corepack \$(corepack --version)
bwrap --version
socat -V | sed -n 2p
poetry --version
/usr/local/bin/uv --version
ls /opt/uv-python'"
