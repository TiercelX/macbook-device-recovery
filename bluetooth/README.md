# Bluetooth startup recovery

Optional, boot-time recovery for BCM4350C0 UART Bluetooth on Linux MacBookPro13,1.
This replaces the old single blind initialization retry with bounded retries and status checks.
It is separate from the Wi-Fi / SPI timer and does not provide continuous Bluetooth monitoring.

The service waits 45 seconds at boot, checks controller state, and only reloads `hci_uart`
when the controller is unavailable **and** the current boot has a matching initialization
error in the last three minutes. Healthy controllers, rfkill blocks, deliberate BlueZ
power-off, stopped Bluetooth services, absent drivers, unsupported models, other controllers,
and unclear failures do not trigger a reset. It never explicitly turns Bluetooth power on.

At most three reload attempts are allowed per boot, including failures. The budget is saved
before each attempt and survives service restarts. Backoff is 5, 10, then 15 seconds.
Unloading failure still attempts to restore the driver. Controller state is rechecked after
each attempt; driver reload alone is not reported as recovery. Only `UP RUNNING` proves
controller readiness; actual connections / audio remain unverified. A skip means policy
was respected, not that a fault was fixed.

Existing advertisement compatibility modules are left loaded. This tool does not install,
modify, or replace those patches, Bluetooth firmware, pairing data or network credentials.
Controller addresses and command output are not written to its logs. Local state includes
the current boot ID and attempt count; do not publish runtime state or system journals.

## Install / inspect

Dependencies: Python 3, systemd, BlueZ (`hciconfig`, `bluetoothctl`), kmod, and udev.
From the repository root:

```bash
python3 bluetooth/recover.py --dry-run
sudo bash bluetooth/install.sh
systemctl is-enabled bluetooth-retry.service
journalctl -u bluetooth-retry.service -n 20 --no-pager
```

Installation backs up old files and boot enablement under `/var/backups/mbp-bluetooth-recovery.*`,
then enables `bluetooth-retry.service` for the next boot. It does not start recovery immediately.
For a deliberate immediate run, `sudo systemctl start bluetooth-retry.service`; this may briefly
disconnect Bluetooth devices. Do not run it merely to test a healthy controller.

Rollback using the exact backup path printed during installation:

```bash
sudo bash bluetooth/rollback.sh /var/backups/mbp-bluetooth-recovery.BACKUP_ID
```

Rollback restores prior files and boot enablement without starting recovery. Existing
legacy scripts, the recovery state and backups are retained.

## Tests

```bash
python3 -m unittest discover -s bluetooth -p 'test_bluetooth_recovery.py'
```

Tests simulate errors and policy states; they do not reload hardware. Startup fault recovery
still needs validation on a real failed boot. There is no periodic timer or reset on a merely
disconnected device.
