#!/bin/bash
set -euo pipefail
[[ $EUID == 0 && $# == 1 ]] || { echo "Run: sudo bash $0 BACKUP_DIRECTORY" >&2; exit 1; }
backup=$(realpath -- "$1")
[[ $backup == /var/backups/mbp-bluetooth-recovery.* && -d $backup && -f $backup/enabled && -f $backup/active ]] || { echo 'Invalid backup directory' >&2; exit 1; }
systemctl disable --now bluetooth-retry.service
for target in /etc/systemd/system/bluetooth-retry.service /usr/local/libexec/mbp-bluetooth-recovery.py; do
    if [[ -f $backup$target ]]; then
        install -D -m "$([[ $target == *.py ]] && echo 755 || echo 644)" "$backup$target" "$target"
    else
        rm -f -- "$target"
    fi
done
if [[ -f $backup/usr/local/bin/bluetooth-retry.sh ]]; then
    install -D -m 755 "$backup/usr/local/bin/bluetooth-retry.sh" /usr/local/bin/bluetooth-retry.sh
fi
systemctl daemon-reload
if [[ $(cat "$backup/enabled") == enabled ]]; then systemctl enable bluetooth-retry.service; fi
echo 'Previous files and boot enablement restored; not starting recovery now.'
