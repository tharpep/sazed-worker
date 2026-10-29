#!/usr/bin/env bash
# Install the tools the worker's checks and (later) the build sandbox need on CT 110:
#
#   ./install-tools.sh
#
# - golang-go: gofmt and go vet for Go repos (plans/10 P1.5)
# - nodejs, npm, node-corepack: tsc after installing a TypeScript repo's packages (pnpm comes
#   through corepack, pinned by the repo's packageManager field)
# - bubblewrap, socat: Claude Code's Bash sandbox on Linux (plans/10 P1.11)
#
# ruff is already in /opt/ruff. Same SSH path as deploy.sh; safe to run again.
set -euo pipefail

PROXMOX="root@192.168.50.111"
SSH_KEY="$HOME/.ssh/proxmox_media"
VMID=110
PACKAGES="golang-go nodejs npm node-corepack bubblewrap socat"

ssh -i "$SSH_KEY" -o BatchMode=yes "$PROXMOX" "pct exec $VMID -- sh -c 'set -e
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends $PACKAGES >/dev/null
go version
gofmt -h 2>&1 | head -1 || true
echo node \$(node --version), npm \$(npm --version), corepack \$(corepack --version)
bwrap --version
socat -V | sed -n 2p'"
