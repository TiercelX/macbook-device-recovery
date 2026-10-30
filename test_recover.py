import unittest
from unittest.mock import patch
import recover


class DetectionTest(unittest.TestCase):
    def row(self, stamp, message):
        return {'__MONOTONIC_TIMESTAMP': str(int(stamp * 1_000_000)), 'MESSAGE': message}

    def test_current_faults_and_no_replay(self):
        rows = [self.row(100, 'brcmf_cfg80211_scan: scan error (-12)'),
                self.row(101, 'brcmf_cfg80211_scan: scan error (-12)'),
                self.row(102, 'applespi spi3.0: Received corrupted packet (invalid message length 8)'),
                self.row(104, 'brcmf_cfg80211_scan: scan error (-12)')]
        self.assertEqual(recover.counts(rows, 100, 103), {'wifi': 1, 'input': 1})

    def test_unrelated_errors_do_not_reset_hardware(self):
        rows = [self.row(101, s) for s in ['firmware brcmfmac file failed with error -2',
                'brcmf_cfg80211_scan: scan error (-16)', 'input: Apple SPI Touchpad',
                'Bluetooth: unknown advertising packet type', 'no input activity']]
        self.assertEqual(recover.counts(rows, 100, 102), {'wifi': 0, 'input': 0})

    def test_cooldown_and_hourly_budget(self):
        self.assertTrue(recover.eligible([], 4000))
        self.assertFalse(recover.eligible([3900], 4000))
        self.assertTrue(recover.eligible([3700], 4000))
        self.assertFalse(recover.eligible([1000, 2000, 3000], 4000))
        self.assertTrue(recover.eligible([400, 2000, 3000], 4000))

    def test_no_journal_matches_is_healthy(self):
        error = recover.subprocess.CalledProcessError(1, 'journalctl', output='', stderr='')
        with patch.object(recover, 'run', side_effect=error):
            self.assertEqual(recover.recent_records(), [])

    def test_journal_failure_does_not_look_healthy(self):
        error = recover.subprocess.CalledProcessError(1, 'journalctl', output='', stderr='Permission denied')
        with patch.object(recover, 'run', side_effect=error):
            with self.assertRaises(recover.subprocess.CalledProcessError):
                recover.recent_records()

    def test_reload_restores_module_after_unload_failure(self):
        calls = []
        def fake_run(args, **kwargs):
            calls.append(args)
            if args == ['/usr/sbin/modprobe', '-r', 'brcmfmac']:
                raise recover.subprocess.CalledProcessError(1, args)
        with patch.object(recover, 'run', side_effect=fake_run), patch.object(recover.Path, 'exists', return_value=True):
            with self.assertRaises(recover.subprocess.CalledProcessError):
                recover.reload_device('wifi')
        self.assertEqual(calls, [['/usr/sbin/modprobe', '-r', 'brcmfmac_wcc'],
                                 ['/usr/sbin/modprobe', '-r', 'brcmfmac'],
                                 ['/usr/sbin/modprobe', 'brcmfmac']])


class PciRecoveryTest(unittest.TestCase):
    def records(self, stamp=100):
        return [{'__MONOTONIC_TIMESTAMP': str(stamp * 1000000), 'MESSAGE': m} for m in (
            'brcmfmac: brcmf_chip_recognition: MMIO read failed: 0xffffffff',
            'brcmfmac: brcmf_pcie_probe: failed 14e4:43a3')]

    def test_requires_both_exact_recent_faults(self):
        self.assertTrue(recover.init_fault(self.records(), 110))
        self.assertFalse(recover.init_fault(self.records()[:1], 110))
        self.assertFalse(recover.init_fault(self.records(), 300))
        self.assertFalse(recover.init_fault(self.records(120), 110))

    def test_boot_budget_cooldown_and_safety_gates(self):
        for attempted, safe, allowed, attempts in (
                (True, True, True, []), (False, False, True, []),
                (False, True, False, []), (False, True, True, [100])):
            state = {'attempts': {'wifi': attempts}, 'wifi_pci_attempted': attempted}
            with patch.object(recover, 'pci_safe', return_value=safe), patch.object(recover, 'wifi_allowed', return_value=allowed), patch.object(recover, 'reset_wifi_pci') as reset:
                recover.recover_init_fault(state, self.records(), 110)
                reset.assert_not_called()

    def test_failed_reset_consumes_budget_and_does_not_replay(self):
        state = {'attempts': {}}
        with patch.object(recover, 'pci_safe', return_value=True), patch.object(recover, 'wifi_allowed', return_value=True), patch.object(recover, 'save') as save, patch.object(recover, 'reset_wifi_pci', side_effect=RuntimeError('simulated failure')) as reset:
            recover.recover_init_fault(state, self.records(), 110)
            recover.recover_init_fault(state, self.records(), 111)
            self.assertEqual(reset.call_count, 1)
            self.assertTrue(state['wifi_pci_attempted'])
            self.assertEqual(state['attempts']['wifi'], [110])
            self.assertGreaterEqual(save.call_count, 2)

    def test_pci_topology_identity_and_healthy_device(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            target = parent / '0000:02:00.0'
            target.mkdir()
            for key, value in {'vendor': '0x14e4', 'device': '0x43a3', 'reset': '', 'reset_method': 'bus'}.items():
                (target / key).write_text(value)
            self.assertTrue(recover.pci_safe(target))
            sibling = parent / '0000:02:00.1'
            sibling.mkdir()
            self.assertFalse(recover.pci_safe(target))
            sibling.rmdir()
            (target / 'driver').mkdir()
            self.assertFalse(recover.pci_safe(target))
            (target / 'driver').rmdir()
            (target / 'net').mkdir()
            (target / 'net' / 'wlan0').mkdir()
            self.assertFalse(recover.pci_safe(target))
            (target / 'net' / 'wlan0').rmdir()
            (target / 'vendor').write_text('0x1234')
            self.assertFalse(recover.pci_safe(target))

    def test_successful_reset_checks_interface_and_restores_module(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / 'reset').write_text('')
            calls = []
            def fake_run(args, **kwargs):
                calls.append(args)
                if args == ['/usr/sbin/modprobe', 'brcmfmac']:
                    (target / 'driver').mkdir()
                    (target / 'net' / 'wlan0').mkdir(parents=True)
            with patch.object(recover, 'pci_safe', return_value=True), patch.object(recover, 'run', side_effect=fake_run):
                recover.reset_wifi_pci(target)
            self.assertEqual((target / 'reset').read_text(), '1\n')
            self.assertIn(['/usr/sbin/modprobe', '-r', 'brcmfmac'], calls)
            self.assertIn(['/usr/sbin/modprobe', 'brcmfmac'], calls)

    def test_failed_pci_write_still_reloads_driver(self):
        from pathlib import Path
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            # No reset file's parent: the write must fail after module removal.
            target = Path(tmp) / 'missing'
            with patch.object(recover, 'pci_safe', return_value=True), patch.object(recover, 'run') as run:
                with self.assertRaises(OSError):
                    recover.reset_wifi_pci(target)
                self.assertEqual(run.call_args.args[0], ['/usr/sbin/modprobe', 'brcmfmac'])


if __name__ == '__main__':
    unittest.main()
