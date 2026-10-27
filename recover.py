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
        output = run(['/usr/bin/journalctl', '-k', '-b', '--since', '2 minutes ago',
                      '--no-pager', '-o', 'json', '-n', '512', '--grep',
                      'brcmf_cfg80211_scan: scan error|applespi.*Received corrupted packet']).stdout
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
    if state.get('boot') != boot:
        state = {'boot': boot, 'checked': now, 'attempts': {}}
        if not args.dry_run:
            # First activation must not replay earlier failures that were fixed manually.
            save(state)
            print('initialized; ignoring faults from before activation', flush=True)
            return
    after = max(state.get('checked', now), now - 90)
    if args.dry_run:
        after = now - 90
    # Filter before limiting output, so unrelated kernel logs cannot hide our faults.
    found = counts(recent_records(), after, now)
    if args.dry_run:
        print(json.dumps({'last_90s_errors': found, 'threshold': THRESHOLD, 'action': 'none (dry run)'}, ensure_ascii=False))
        return
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
