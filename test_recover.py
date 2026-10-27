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


if __name__ == '__main__':
    unittest.main()
