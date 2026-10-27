#!/bin/bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo "Run: sudo bash $0" >&2; exit 1; }
base=$(cd -- "$(dirname -- "$0")" && pwd)
for executable in python3 hciconfig bluetoothctl modprobe journalctl systemctl udevadm; do
    command -v "$executable" >/dev/null || { echo "Missing dependency: $executable" >&2; exit 1; }
done
[[ $(cat /sys/class/dmi/id/product_name) == MacBookPro13,1 ]] || { echo 'Unsupported model' >&2; exit 1; }
backup=$(mktemp -d /var/backups/mbp-bluetooth-recovery.XXXXXXXX)
for target in /etc/systemd/system/bluetooth-retry.service /usr/local/bin/bluetooth-retry.sh /usr/local/libexec/mbp-bluetooth-recovery.py; do
    if [[ -e $target ]]; then cp -a --parents "$target" "$backup/"; fi
done
systemctl is-enabled bluetooth-retry.service >"$backup/enabled" 2>/dev/null || true
systemctl is-active bluetooth-retry.service >"$backup/active" 2>/dev/null || true
systemd-analyze verify "$base/bluetooth-retry.service"
if systemctl cat bluetooth-retry.service >/dev/null 2>&1; then systemctl stop bluetooth-retry.service; fi
install -d -m 755 /usr/local/libexec
install -m 755 "$base/recover.py" /usr/local/libexec/mbp-bluetooth-recovery.py
install -m 644 "$base/bluetooth-retry.service" /etc/systemd/system/bluetooth-retry.service
systemctl daemon-reload
systemctl enable bluetooth-retry.service
printf 'Installed for next boot; no reset performed. Backup: %s\n' "$backup"
printf 'Rollback: sudo bash %s/rollback.sh %s\n' "$base" "$backup"
