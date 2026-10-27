#!/bin/bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo "请执行：sudo bash $0" >&2; exit 1; }
systemctl disable --now mbp-device-recovery.timer
systemctl stop mbp-device-recovery.service
rm -f /etc/systemd/system/mbp-device-recovery.service /etc/systemd/system/mbp-device-recovery.timer /usr/local/libexec/mbp-device-recovery.py
systemctl daemon-reload
echo '已撤销；日志、状态及 /var/backups/mbp-device-recovery.* 备份保留。'
