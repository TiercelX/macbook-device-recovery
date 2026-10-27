# MacBook device recovery

Bounded Linux driver recovery for Broadcom BCM4350 Wi-Fi (`brcmfmac`) and Apple SPI keyboard/trackpad (`applespi`), developed for MacBookPro13,1.

Optional [Bluetooth startup recovery](bluetooth/README.md) is installed separately. It provides bounded UART initialization retries, respects radio-off policy, and leaves existing advertisement compatibility patches in place.

A systemd timer checks recent kernel errors every 30 seconds. Three matching errors in a rolling 90-second window trigger a reload:

- Wi-Fi: `brcmf_cfg80211_scan: scan error (-12)`. Unload `brcmfmac_wcc` before reloading `brcmfmac`; NetworkManager handles reconnecting with existing profiles. A disabled Wi-Fi radio is respected.
- Keyboard/trackpad: `applespi ... Received corrupted packet`. Reloading `applespi` briefly disconnects both built-in input devices.

Each device has a five-minute cooldown and at most three attempts per hour, including failed attempts. State survives service restarts within the current boot. First activation ignores old faults. The tool reads kernel error logs, never key contents or input events. A silent freeze, repeated characters without matching errors, or ordinary system load does not trigger recovery. Driver reload / device registration alone does not prove usability. This is a recovery mechanism, not a diagnosis of the underlying hardware or firmware fault.

## Install

Requirements: Linux with systemd, Python 3, NetworkManager (`nmcli`), `modprobe`, and `udevadm`; the relevant drivers must already be available. Inspect scripts before installing, and keep an alternate input / network path available for initial checks.

From this directory:

```bash
sudo bash ./install.sh
```

The installer copies the tool into `/usr/local/libexec/`, installs the units, backs up any previous files under `/var/backups/mbp-device-recovery.*`, and enables the timer. Initial execution establishes a baseline without resetting devices.

## Status

```bash
systemctl status mbp-device-recovery.timer
journalctl -u mbp-device-recovery.service -n 20 --no-pager
python3 ./recover.py --dry-run
```

Dry-run reports recent matching errors without changing state or reloading drivers. Normal checks are quiet; recovery decisions are logged to the service journal. The timer being active only proves scheduling; check the service result and real device behavior to assess recovery.

## Remove

```bash
sudo bash ./uninstall.sh
```

Removal stops and disables the timer, then removes the installed program and units. Local state, journals and backups are retained. To restore an older version, stop both units, restore the `usr/` and `etc/` files from the corresponding backup directory, run `systemctl daemon-reload`, and restore the timer state recorded in the backup.

## Tests

```bash
python3 -m unittest discover -s . -p 'test_recover.py'
```

Tests cover fault discrimination, stale-record exclusion, cooldown and hourly limits, journal failures, and module restoration after unload failure. They do not reset hardware. Runtime state, credentials, network profiles and machine identifiers are not part of the repository.
