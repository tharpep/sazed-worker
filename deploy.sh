#!/usr/bin/env bash
# Deploy worker.py, the sazed_worker package and the systemd unit to CT 110 and restart the
# service.
#
#   ./deploy.sh
#
# Copies over SSH through the Proxmox host instead of using a GitHub runner, so CT 110 holds no
# runner credentials. Needs ~/.ssh/proxmox_media (root on the Proxmox host). Run from Git Bash.
set -euo pipefail
cd "$(dirname "$0")"

PROXMOX="root@192.168.50.111"
SSH_KEY="$HOME/.ssh/proxmox_media"
VMID=110

python -m compileall -q worker.py sazed_worker
tar -cf - worker.py sazed_worker/*.py sazed-worker.service | ssh -i "$SSH_KEY" -o BatchMode=yes \
  "$PROXMOX" "pct exec $VMID -- sh -c 'set -e
mkdir -p /opt/sazed-worker
rm -rf /opt/sazed-worker/sazed_worker
tar --no-same-owner -xf - -C /opt/sazed-worker
chmod 755 /opt/sazed-worker/sazed_worker
chmod 644 /opt/sazed-worker/worker.py /opt/sazed-worker/sazed_worker/*.py
mv /opt/sazed-worker/sazed-worker.service /etc/systemd/system/sazed-worker.service
systemctl daemon-reload
systemctl enable --quiet sazed-worker
systemctl restart sazed-worker
sleep 3
systemctl is-active sazed-worker
journalctl -u sazed-worker -n 5 --no-pager'"
