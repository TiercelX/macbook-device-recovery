# MacBook device recovery

Bounded Linux driver recovery for Broadcom BCM4350 Wi-Fi (`brcmfmac`) and Apple SPI keyboard/trackpad (`applespi`), developed for MacBookPro13,1.

Optional [Bluetooth startup recovery](bluetooth/README.md) is installed separately. It provides bounded UART initialization retries, respects radio-off policy, and leaves existing advertisement compatibility patches in place.

A systemd timer checks recent kernel errors every 30 seconds. For scan and input faults, three matching errors in a rolling 90-second window trigger a reload:

- Wi-Fi: `brcmf_cfg80211_scan: scan error (-12)`. Unload `brcmfmac_wcc` before reloading `brcmfmac`; NetworkManager handles reconnecting with existing profiles. A disabled Wi-Fi radio is respected.
- Keyboard/trackpad: `applespi ... Received corrupted packet`. Reloading `applespi` briefly disconnects both built-in input devices.

Wi-Fi initialization recovery also handles the combination of `brcmf_chip_recognition: MMIO read failed: 0xffffffff` and `brcmf_pcie_probe: failed 14e4:43a3` within the last three minutes of the current boot. Before resetting anything, it requires all of the following:

- The Wi-Fi radio is enabled and no WLAN rfkill switch is blocked.
- The endpoint at PCI slot `0000:02:00.0` is a Broadcom BCM4350 (`14e4:43a3`), has no bound driver or network interface, and supports bus reset.
- The endpoint is alone under its upstream PCI branch. A shared branch is skipped.

The tool unloads the Wi-Fi driver, rechecks the endpoint, resets its PCI branch, and reloads the driver even if resetting fails. It checks that the driver and interface return; NetworkManager handles reconnection. PCI reset is limited to **one attempt per boot**, including failures, and also consumes the existing Wi-Fi cooldown and hourly budget. The slot is deliberately fixed for the supported layout; other layouts are skipped. The service grants sysfs write access only to that endpoint's `reset` file while keeping other kernel tunables read-only.

Each device has a five-minute cooldown and at most three attempts per hour, including failed attempts. State survives service restarts within the current boot. First activation ignores old scan/input faults. Recent initialization faults can be handled on the first check only if the endpoint is still unbound and has no interface. The tool reads kernel error logs, never key contents or input events. A silent freeze, repeated characters without matching errors, or ordinary system load does not trigger recovery. Driver reload / device registration alone does not prove usability. This is a recovery mechanism, not a diagnosis of the underlying hardware or firmware fault.

## Install

Requirements: Linux with systemd, Python 3, NetworkManager (`nmcli`), `modprobe`, and `udevadm`; the relevant drivers must already be available. Inspect scripts before installing, and keep an alternate input / network path available for initial checks.

From this directory:

```bash
sudo bash ./install.sh
```

The installer copies the tool into `/usr/local/libexec/`, installs the units, backs up any previous files under `/var/backups/mbp-device-recovery.*`, and enables the timer. Initial execution establishes a baseline for scan/input faults. It can also recover a recent, still-present Wi-Fi initialization failure using the checks above.

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

Tests cover fault discrimination, stale-record exclusion, cooldown and hourly limits, journal failures, PCI identity and topology checks, healthy-interface exclusion, per-boot reset limits, and module restoration after unload/reset failure. They do not reset hardware. Runtime state, credentials, network profiles and machine identifiers are not part of the repository.

Manual PCI reset followed by driver reload has restored this observed MMIO initialization failure. The automated decision and failure paths are covered by simulations; recovery during a later faulty boot has not yet been observed.
