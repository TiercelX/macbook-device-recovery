#!/usr/bin/python3
"""Bounded recovery of known BCM4350 / Apple SPI kernel faults.

Only kernel error messages are inspected; input events and key contents are not read.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import time

STATE = Path('/var/lib/mbp-device-recovery/state.json')
COOLDOWN = 300
LIMIT = 3
THRESHOLD = 3
PATTERNS = {
    'wifi': re.compile(r'brcmf_cfg80211_scan: scan error \(-12\)'),
    'input': re.compile(r'applespi.*Received corrupted packet'),
}


def run(args, timeout=20):
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=True)


def counts(records, after, before):
    result = dict.fromkeys(PATTERNS, 0)
    for row in records:
        stamp = int(row.get('__MONOTONIC_TIMESTAMP', 0)) / 1_000_000
        if after < stamp <= before:
            message = row.get('MESSAGE', '')
            if isinstance(message, str):
                for device, pattern in PATTERNS.items():
                    result[device] += bool(pattern.search(message))
    return result


def recent_records():
    try:
        output = run(['/usr/bin/journalctl', '-k', '-b', '--since', '3 minutes ago',
                      '--no-pager', '-o', 'json', '-n', '512', '--grep',
                      'brcmf_cfg80211_scan: scan error|applespi.*Received corrupted packet|brcmf_chip_recognition: MMIO read failed|brcmf_pcie_probe: failed']).stdout
    except subprocess.CalledProcessError as exc:
        # journalctl --grep returns 1 for no matches, even on a healthy system.
        if exc.returncode == 1 and not exc.stdout.strip() and not exc.stderr.strip():
            return []
        raise
    return [json.loads(line) for line in output.splitlines() if line]


def eligible(attempts, now):
    recent = [t for t in attempts if now - 3600 < t <= now]
    return len(recent) < LIMIT and (not recent or now - max(recent) >= COOLDOWN)


def reload_device(device):
    module = 'brcmfmac' if device == 'wifi' else 'applespi'
    # Even if unloading the main module fails, try to restore any removed module.
    try:
        if device == 'wifi' and Path('/sys/module/brcmfmac_wcc').exists():
            run(['/usr/sbin/modprobe', '-r', 'brcmfmac_wcc'])
        run(['/usr/sbin/modprobe', '-r', module])
    finally:
        run(['/usr/sbin/modprobe', module])
    run(['/usr/bin/udevadm', 'settle', '--timeout=10'])
    if not Path('/sys/module', module).exists():
        raise RuntimeError(f'{module} missing after reload')
    if device == 'wifi':
        # NetworkManager owns reconnection using existing profiles. No new credentials.
        status = run(['/usr/bin/nmcli', '-t', '-f', 'DEVICE,TYPE,STATE', 'device']).stdout.strip()
        print(f'wifi reload completed; NetworkManager state: {status}', flush=True)
    else:
        names = [p.read_text().strip() for p in Path('/sys/bus/spi/drivers/applespi').glob('spi*/input/input*/name')]
        if not any('Touchpad' in name for name in names) or not any('Keyboard' in name for name in names):
            raise RuntimeError(f'Apple input devices not both registered: {names}')
        print('input reload completed; keyboard and touchpad registered (usability not verified)', flush=True)


# This machine's internal BCM4350 only; the unit grants write access to this reset.
PCI = Path('/sys/bus/pci/devices/0000:02:00.0')


def init_fault(records, now):
    messages = [r.get('MESSAGE', '') for r in records
                if now - 180 < int(r.get('__MONOTONIC_TIMESTAMP', 0)) / 1_000_000 <= now
                and isinstance(r.get('MESSAGE'), str)]
    return (any('brcmf_chip_recognition: MMIO read failed: 0xffffffff' in m for m in messages)
            and any('brcmf_pcie_probe: failed 14e4:43a3' in m for m in messages))


def pci_safe(target=PCI):
    # A bus reset can affect siblings: require this endpoint to own its branch.
    if (target / 'vendor').read_text().strip() != '0x14e4' or (target / 'device').read_text().strip() != '0x43a3':
        return False
    endpoint = target.resolve(strict=True)
    if list(endpoint.parent.glob('????:??:??.?')) != [endpoint]:
        return False
    if (target / 'driver').exists() or list((target / 'net').glob('*')):
        return False
    return ((target / 'reset').exists()
            and (target / 'reset_method').read_text().strip() == 'bus')


def wifi_allowed():
    if run(['/usr/bin/nmcli', 'radio', 'wifi']).stdout.strip() != 'enabled':
        return False
    for radio in Path('/sys/class/rfkill').glob('rfkill*'):
        if (radio / 'type').read_text().strip() == 'wlan':
            if any((radio / flag).read_text().strip() != '0' for flag in ('soft', 'hard')):
                return False
    return True


def reset_wifi_pci(target=PCI):
    try:
        if Path('/sys/module/brcmfmac_wcc').exists():
            run(['/usr/sbin/modprobe', '-r', 'brcmfmac_wcc'])
        run(['/usr/sbin/modprobe', '-r', 'brcmfmac'])
        # Recheck immediately before writing, after driver removal.
        if not pci_safe(target):
            raise RuntimeError('PCI safety check failed before reset')
        (target / 'reset').write_text('1\n')
    finally:
        run(['/usr/sbin/modprobe', 'brcmfmac'])
    run(['/usr/bin/udevadm', 'settle', '--timeout=10'])
    for _ in range(10):
        if (target / 'driver').exists() and list((target / 'net').glob('*')):
            print('wifi: PCI reset restored driver and interface; NetworkManager owns reconnection', flush=True)
            return
        time.sleep(1)
    raise RuntimeError('PCI reset did not restore wireless interface')


def recover_init_fault(state, records, now):
    if state.get('wifi_pci_attempted') or not init_fault(records, now):
        return
    try:
        if not wifi_allowed() or not pci_safe():
            return
        attempts = state['attempts'].get('wifi', [])
        if not eligible(attempts, now):
            return
        # One PCI reset per boot; failed attempts consume both budgets.
        state['wifi_pci_attempted'] = True
        state['attempts']['wifi'] = [t for t in attempts if t > now - 3600] + [now]
        save(state)
        print('wifi: current MMIO/probe failure and absent interface; resetting isolated PCI branch', flush=True)
        try:
            reset_wifi_pci()
        finally:
            state['checked'] = time.clock_gettime(time.CLOCK_MONOTONIC)
            save(state)
    except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
        print(f'wifi: PCI recovery failed/skipped: {exc}', flush=True)


def save(state):
    temp = STATE.with_suffix('.tmp')
    temp.write_text(json.dumps(state) + '\n')
    temp.replace(STATE)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='report recent errors without reload or state writes')
    args = parser.parse_args()
    if not args.dry_run and os.geteuid() != 0:
        parser.error('root required; use --dry-run for read-only diagnosis')
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    now = time.clock_gettime(time.CLOCK_MONOTONIC)
    try:
        state = {} if args.dry_run else json.loads(STATE.read_text())
    except FileNotFoundError:
        state = {}
    records = recent_records()
    if state.get('boot') != boot:
        state = {'boot': boot, 'checked': now, 'attempts': {}}
        if not args.dry_run:
            # Scan/input faults retain their baseline. Boot MMIO faults require
            # the endpoint to still be unbound and have no interface.
            save(state)
            recover_init_fault(state, records, now)
            print('initialized; ignoring earlier scan/input faults', flush=True)
            return
    after = max(state.get('checked', now), now - 90)
    if args.dry_run:
        after = now - 90
    # Filter before limiting output, so unrelated kernel logs cannot hide our faults.
    found = counts(records, after, now)
    if args.dry_run:
        print(json.dumps({'last_90s_errors': found, 'threshold': THRESHOLD, 'recent_mmio_probe_fault': init_fault(records, now), 'action': 'none (dry run)'}, ensure_ascii=False))
        return
    recover_init_fault(state, records, now)
    # Keep a rolling 90-second window anchored at activation / last recovery.
    # Clearing it on each 30-second check would miss faults spaced 15s apart.
    for device, count in found.items():
        if count < THRESHOLD:
            continue
        attempts = state['attempts'].get(device, [])
        if not eligible(attempts, now):
            print(f'{device}: {count} errors; blocked by 5-minute cooldown / 3 attempts per hour', flush=True)
            continue
        if device == 'wifi':
            if run(['/usr/bin/nmcli', 'radio', 'wifi']).stdout.strip() != 'enabled':
                print('wifi: radio disabled; respecting user choice', flush=True)
                continue
            if not Path('/sys/module/brcmfmac').exists():
                print('wifi: brcmfmac absent; skipping stale fault', flush=True)
                continue
        elif not Path('/sys/module/applespi').exists():
            print('input: applespi absent; skipping stale fault', flush=True)
            continue
        # Count failed recovery attempts too, and persist before touching hardware.
        state['attempts'][device] = [t for t in attempts if t > now - 3600] + [now]
        save(state)
        print(f'{device}: {count} known errors; reloading driver', flush=True)
        try:
            reload_device(device)
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            print(f'{device}: recovery failed: {exc}; {getattr(exc, "stderr", "")}', flush=True)
        finally:
            # Ignore logs produced during this reload; only later faults may retry.
            state['checked'] = time.clock_gettime(time.CLOCK_MONOTONIC)
            save(state)


if __name__ == '__main__':
    main()
