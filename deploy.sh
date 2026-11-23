#!/usr/bin/env bash
# Deploy worker.py, the sazed_worker package and the systemd unit to CT 110 and restart the
# service.
#
#   ./deploy.sh
#
# Copies over SSH through the Proxmox host instead of using a GitHub runner, so CT 110 holds no
# runner credentials. Needs ~/.ssh/proxmox_media (root on the Proxmox host) and PROXMOX_SSH
# (user@host of the Proxmox host), from the environment or an untracked .deploy.env beside this
# script. Run from Git Bash.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .deploy.env ] && . ./.deploy.env
PROXMOX="${PROXMOX_SSH:?set PROXMOX_SSH to user@host of the Proxmox host}"
SSH_KEY="$HOME/.ssh/proxmox_media"
VMID=110
ENV_FILE=/etc/sazed-eco/sazed-worker.env

# The unit no longer carries SAZED_URL, so a worker restarted without it in the env file would
# exit at startup. Refuse before copying anything.
ssh -i "$SSH_KEY" -o BatchMode=yes "$PROXMOX" "pct exec $VMID -- grep -q '^SAZED_URL=' $ENV_FILE" \
  || { echo "refused: SAZED_URL is not set in $ENV_FILE on CT $VMID" >&2; exit 1; }

python -m compileall -q worker.py sazed_worker
find sazed_worker -name '*.py' -not -path '*/__pycache__/*' -print0 \
  | tar -cf - --null -T - worker.py sazed-worker.service \
  | ssh -i "$SSH_KEY" -o BatchMode=yes \
  "$PROXMOX" "pct exec $VMID -- sh -c 'set -e
mkdir -p /opt/sazed-worker
rm -rf /opt/sazed-worker/sazed_worker
tar --no-same-owner -xf - -C /opt/sazed-worker
chmod 755 /opt/sazed-worker
chmod 644 /opt/sazed-worker/worker.py
find /opt/sazed-worker/sazed_worker -type d -exec chmod 755 {} +
find /opt/sazed-worker/sazed_worker -type f -exec chmod 644 {} +
mv /opt/sazed-worker/sazed-worker.service /etc/systemd/system/sazed-worker.service
systemctl daemon-reload
systemctl enable --quiet sazed-worker
systemctl restart sazed-worker
sleep 3
systemctl is-active sazed-worker
journalctl -u sazed-worker -n 5 --no-pager'"
