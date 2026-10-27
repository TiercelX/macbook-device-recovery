#!/bin/bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo "请执行：sudo bash $0" >&2; exit 1; }
base=$(cd -- "$(dirname -- "$0")" && pwd)
backup=$(mktemp -d /var/backups/mbp-device-recovery.XXXXXXXX)
for target in /usr/local/libexec/mbp-device-recovery.py /etc/systemd/system/mbp-device-recovery.{service,timer}; do
    if [[ -e $target ]]; then cp -a --parents "$target" "$backup/"; fi
done
systemctl is-enabled mbp-device-recovery.timer >"$backup/timer-enabled" 2>/dev/null || true
systemctl is-active mbp-device-recovery.timer >"$backup/timer-active" 2>/dev/null || true
echo "原文件备份：$backup"
for unit in mbp-device-recovery.timer mbp-device-recovery.service; do
    if systemctl cat "$unit" >/dev/null 2>&1; then systemctl stop "$unit"; fi
done
install -d -m 755 /usr/local/libexec
install -m 755 "$base/recover.py" /usr/local/libexec/mbp-device-recovery.py
install -m 644 "$base/mbp-device-recovery.service" "$base/mbp-device-recovery.timer" /etc/systemd/system/
systemctl daemon-reload
# First check establishes the baseline without resetting devices.
systemctl start mbp-device-recovery.service
systemctl enable --now mbp-device-recovery.timer
systemctl --no-pager status mbp-device-recovery.timer
echo "已启用；撤销：sudo bash $base/uninstall.sh"
