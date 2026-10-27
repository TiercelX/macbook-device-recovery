#!/usr/bin/python3
"""Bounded boot-time recovery for the BCM4350C0 UART Bluetooth controller."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import time

STATE_DIR = Path('/var/lib/mbp-bluetooth-recovery')
SYS = Path('/sys')
KNOWN_FAULT = r'hci[0-9]+: (BCM: (Reset failed|failed to write update baudrate)|command .*tx timeout|Opcode .*failed)'
MAX_ATTEMPTS = 3


def command(args, timeout=15):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def checked(args):
    result = command(args)
    if result.returncode:
        # Avoid logging command output; Bluetooth tools can include device addresses.
        raise RuntimeError(f'{Path(args[0]).name} operation failed (exit {result.returncode})')
    return result


def blocked():
    for path in (SYS / 'class/rfkill').glob('rfkill*'):
        if (path / 'type').read_text().strip() == 'bluetooth':
            if any((path / flag).read_text().strip() == '1' for flag in ('soft', 'hard')):
                return True
    return False


def fault_logged():
    result = command(['journalctl', '-k', '-b', '--since', '3 minutes ago',
                      '--no-pager', '-o', 'cat', '-n', '32', '--grep', KNOWN_FAULT])
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
        return False
    if result.returncode:
        raise RuntimeError('kernel journal unavailable; refusing blind reset')
    return bool(result.stdout.strip())


def assess():
    if (SYS / 'class/dmi/id/product_name').read_text().strip() != 'MacBookPro13,1':
        return 'skip', 'unsupported machine model'
    if blocked():
        return 'skip', 'Bluetooth blocked by user or hardware'
    daemon = command(['systemctl', 'is-active', 'bluetooth.service'])
    if daemon.stdout.strip() != 'active':
        return 'skip', 'Bluetooth service is not active'
    if not (SYS / 'module/hci_uart').exists():
        return 'skip', 'UART driver not loaded; not enabling it automatically'
    adapters = list((SYS / 'class/bluetooth').glob('hci[0-9]*'))
    uart = [p for p in adapters if '/serial' in str(p.resolve())]
    if len(adapters) != len(uart) or len(uart) > 1:
        return 'skip', 'other or multiple controllers present; avoiding shared-service reset'
    if uart:
        result = checked(['hciconfig', uart[0].name])
        if re.search(r'^\s*UP RUNNING\b', result.stdout, re.M):
            return 'healthy', 'UART controller UP RUNNING (connections not verified)'
        address = re.search(r'BD Address:\s*([0-9A-Fa-f:]{17})\b', result.stdout)
        if not address:
            raise RuntimeError('controller identity unavailable; refusing reset')
        show = command(['bluetoothctl', '--timeout', '5', 'show', address[1]], timeout=8)
        if show.returncode == 0 and re.search(r'^\s*Powered:\s*no\s*$', show.stdout, re.M):
            return 'skip', 'controller intentionally powered off; leaving it off'
    if fault_logged():
        return 'fault', 'controller unavailable with recent UART initialization failure'
    return 'unknown', 'controller unavailable without a known recent fault; no reset'


def reload_uart():
    try:
        checked(['modprobe', '-r', 'hci_uart'])
    finally:
        # Restore the driver even if unloading fails; do not touch compatibility modules.
        checked(['modprobe', 'hci_uart'])
    checked(['udevadm', 'settle', '--timeout=10'])
    # Recheck policy before a daemon restart; never turn radio power on explicitly.
    if blocked():
        return
    if not list((SYS / 'class/bluetooth').glob('hci[0-9]*')):
        return
    if command(['systemctl', 'is-active', 'bluetooth.service']).stdout.strip() != 'active':
        return
    # The caller only permits a single UART controller. Recheck hotplug races.
    if any('/serial' not in str(p.resolve()) for p in (SYS / 'class/bluetooth').glob('hci[0-9]*')):
        return
    if assess()[0] != 'fault':
        return
    checked(['systemctl', 'restart', 'bluetooth.service'])


def save(path, state):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(state) + '\n')
    temp.replace(path)


def recover(state_path, boot, inspect=assess, reload=reload_uart, sleep=time.sleep):
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError:
        state = {}
    if state.get('boot') != boot:
        state = {'boot': boot, 'attempts': 0}
    while True:
        status, reason = inspect()
        print(f'Bluetooth: {status}; {reason}', flush=True)
        if status in ('healthy', 'skip'):
            return 0
        if status != 'fault':
            return 1
        if state['attempts'] >= MAX_ATTEMPTS:
            print('Bluetooth: boot recovery budget exhausted; manual diagnosis required', flush=True)
            return 1
        state['attempts'] += 1
        save(state_path, state)
        print(f'Bluetooth: UART reload attempt {state["attempts"]}/{MAX_ATTEMPTS}', flush=True)
        try:
            reload()
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            # Exception text from subprocess may contain controller addresses: log type only.
            print(f'Bluetooth: reload failed ({type(exc).__name__}); rechecking state', flush=True)
        sleep(5 * state['attempts'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    try:
        if args.dry_run:
            status, reason = assess()
            print(f'Bluetooth: {status}; {reason}; action=none')
            return 0 if status in ('healthy', 'skip') else 1
        if os.geteuid() != 0:
            parser.error('root required; use --dry-run for read-only diagnosis')
        STATE_DIR.mkdir(mode=0o700, exist_ok=True)
        with (STATE_DIR / 'lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print('Bluetooth: another recovery is running; skipping', flush=True)
                return 0
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            return recover(STATE_DIR / 'state.json', boot)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f'Bluetooth: check failed ({type(exc).__name__}); no blind retry', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
