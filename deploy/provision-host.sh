#!/usr/bin/env bash
# Prepared root changes on an ALREADY provisioned Ubuntu VM/disk. Never formats a disk.
set -euo pipefail
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mode=${1:-}
topology=${PB_DEPLOY_TOPOLOGY:-single-vm}
case $topology in
  single-vm) ;;
  cloudrun-private-vm)
    python3 "$here/../backend/src/parkingbreaker/deployment_policy.py" \
      "${PB_FRONTEND_SUBNET_CIDR:?}" "${PB_PRIVATE_VM_IP:?}" ;;
  *) echo 'unsupported deployment topology' >&2; exit 2 ;;
esac
firewall_rule() {
  if [[ $mode == --dry-run ]]; then printf 'ufw %s\n' "$*"; else ufw "$@"; fi
}
prepare_rules() {
  firewall_rule default deny incoming
  if [[ $topology == cloudrun-private-vm ]]; then
    firewall_rule allow from "$PB_FRONTEND_SUBNET_CIDR" to "$PB_PRIVATE_VM_IP" port 8443 proto tcp
  else
    firewall_rule allow 80/tcp
    firewall_rule allow 443/tcp
  fi
  firewall_rule allow from 35.235.240.0/20 to any port 22 proto tcp
}
if [[ ${1:-} == --dry-run ]]; then
  printf '%s\n' 'OWNER PLAN: install docker/compose, unattended updates, NTP; SSH keys only; mount existing ext4 regional disk; root-only env; app uid 10001; optional prepared backup timer'
  prepare_rules
  exit 0
fi
if [[ ${1:-} == --enable-firewall ]]; then
  [[ $EUID == 0 && ${PB_IAP_VERIFIED:-} == 1 && ${PB_EXISTING_RULES_REVIEWED:-} == 1 ]] || exit 2
  ufw --force enable
  exit 0
fi
if [[ ${1:-} == --install-backup-timer ]]; then
  [[ $EUID == 0 && $(stat -c '%u:%a' /etc/parkingbreaker/ops.env) == 0:600 ]] || exit 2
  install -m 0644 "$here/systemd/parkingbreaker-backup.service" /etc/systemd/system/
  install -m 0644 "$here/systemd/parkingbreaker-backup.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now parkingbreaker-backup.timer
  exit 0
fi
[[ ${1:-} == --apply && $EUID == 0 ]] || { echo 'owner must run as root with --apply' >&2; exit 2; }
: "${PB_DISK_DEVICE:?existing regional disk device}" "${PB_DISK_MOUNT:?}" "${PB_ENV_SOURCE:?owner-provided env}" "${PB_ENV_FILE:?}"
[[ $PB_DISK_MOUNT == /srv/parkingbreaker && $PB_ENV_FILE == /etc/parkingbreaker/app.env ]]
[[ -b $PB_DISK_DEVICE && -f $PB_ENV_SOURCE && ! -L $PB_ENV_SOURCE ]]
[[ $(stat -c %a "$PB_ENV_SOURCE") == 600 ]]
[[ $(blkid -s TYPE -o value "$PB_DISK_DEVICE") == ext4 ]] || { echo 'existing ext4 required; never format' >&2; exit 2; }
apt-get update
apt-get install -y docker.io docker-compose-v2 unattended-upgrades ufw
systemctl enable --now docker unattended-upgrades
timedatectl set-ntp true
install -d -m 0755 /etc/ssh/sshd_config.d
printf '%s\n' 'PasswordAuthentication no' 'KbdInteractiveAuthentication no' 'PermitRootLogin no' > /etc/ssh/sshd_config.d/00-parkingbreaker.conf
sshd -t
systemctl reload ssh
prepare_rules
# Existing firewall rules must be reviewed by the owner; do not auto-enable/erase them.
printf '%s\n' 'OWNER: verify IAP SSH and existing rules; use --enable-firewall with PB_IAP_VERIFIED=1 PB_EXISTING_RULES_REVIEWED=1. Docker exposes only Caddy.'
cat > /etc/apt/apt.conf.d/52parkingbreaker-updates <<'APT'
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
APT
install -d -m 0700 "$PB_DISK_MOUNT" /etc/parkingbreaker
uuid=$(blkid -s UUID -o value "$PB_DISK_DEVICE")
entry="UUID=$uuid $PB_DISK_MOUNT ext4 defaults 0 2"
if ! findmnt --mountpoint "$PB_DISK_MOUNT" >/dev/null; then
  if ! grep -Fqx "$entry" /etc/fstab; then
    if grep -Fq " $PB_DISK_MOUNT " /etc/fstab; then echo 'conflicting fstab entry' >&2; exit 2; fi
    printf '%s\n' "$entry" >> /etc/fstab
  fi
  mount "$PB_DISK_MOUNT"
fi
findmnt --mountpoint "$PB_DISK_MOUNT" --source "$PB_DISK_DEVICE" >/dev/null
install -d -m 0700 -o 10001 -g 10001 "$PB_DISK_MOUNT/data" "$PB_DISK_MOUNT/tls"
if [[ ! -e $PB_ENV_FILE ]]; then install -m 0600 -o root -g root "$PB_ENV_SOURCE" "$PB_ENV_FILE"; fi
[[ $(stat -c '%u:%a' "$PB_ENV_FILE") == 0:600 && ! -L $PB_ENV_FILE ]]
printf '%s\n' 'Prepared existing host only. Run target validate before lifecycle; no VM/disk/DNS/bucket created.'
