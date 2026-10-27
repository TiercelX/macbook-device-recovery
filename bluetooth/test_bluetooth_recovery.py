import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('bluetooth_recovery', Path(__file__).with_name('recover.py'))
bt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bt)


class BluetoothRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'state.json'

    def test_healthy_or_off_no_reload(self):
        for status in ('healthy', 'skip'):
            reset = Mock()
            self.assertEqual(bt.recover(self.state, 'test-boot', lambda: (status, 'test'), reset, Mock()), 0)
            reset.assert_not_called()
            self.assertFalse(self.state.exists())

    def test_unknown_no_blind_reload(self):
        reset = Mock()
        self.assertEqual(bt.recover(self.state, 'test-boot', lambda: ('unknown', 'test'), reset, Mock()), 1)
        reset.assert_not_called()

    def test_recovery_verifies_state(self):
        inspect = Mock(side_effect=[('fault', 'test'), ('healthy', 'test')])
        reset = Mock()
        self.assertEqual(bt.recover(self.state, 'test-boot', inspect, reset, Mock()), 0)
        reset.assert_called_once()
        self.assertEqual(json.loads(self.state.read_text())['attempts'], 1)

    def test_failed_attempts_count_and_service_restart_cannot_bypass_budget(self):
        reset = Mock(side_effect=RuntimeError('test error'))
        inspect = lambda: ('fault', 'test')
        self.assertEqual(bt.recover(self.state, 'test-boot', inspect, reset, Mock()), 1)
        self.assertEqual(reset.call_count, 3)
        reset.reset_mock()
        self.assertEqual(bt.recover(self.state, 'test-boot', inspect, reset, Mock()), 1)
        reset.assert_not_called()

    def test_new_boot_resets_budget(self):
        self.state.write_text(json.dumps({'boot': 'old-boot', 'attempts': 3}))
        inspect = Mock(side_effect=[('fault', 'test'), ('healthy', 'test')])
        reset = Mock()
        self.assertEqual(bt.recover(self.state, 'new-boot', inspect, reset, Mock()), 0)
        reset.assert_called_once()

    def test_unload_failure_still_restores_driver(self):
        with patch.object(bt, 'checked', side_effect=[RuntimeError('in use'), None]) as cmd:
            with self.assertRaises(RuntimeError):
                bt.reload_uart()
        self.assertEqual(cmd.call_args_list[0].args[0], ['modprobe', '-r', 'hci_uart'])
        self.assertEqual(cmd.call_args_list[1].args[0], ['modprobe', 'hci_uart'])

    def setup_sysfs(self, blocked=False, uart=True):
        self.sys = self.root / 'sys'
        def put(name, data):
            path = self.sys / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data)
        put('class/dmi/id/product_name', 'MacBookPro13,1')
        put('class/rfkill/rfkill0/type', 'bluetooth')
        put('class/rfkill/rfkill0/soft', '1' if blocked else '0')
        put('class/rfkill/rfkill0/hard', '0')
        (self.sys / 'module/hci_uart').mkdir(parents=True)
        (self.sys / 'class/bluetooth').mkdir(parents=True)
        target = self.sys / ('devices/serial0/hci0' if uart else 'devices/usb/hci0')
        target.mkdir(parents=True)
        (self.sys / 'class/bluetooth/hci0').symlink_to(target)

    def test_rfkill_respected(self):
        self.setup_sysfs(blocked=True)
        with patch.object(bt, 'SYS', self.sys), patch.object(bt, 'command') as cmd:
            self.assertEqual(bt.assess()[0], 'skip')
            cmd.assert_not_called()

    def test_user_power_off_respected_even_with_old_errors(self):
        self.setup_sysfs()
        active = subprocess.CompletedProcess([], 0, 'active\n')
        off = subprocess.CompletedProcess([], 0, 'Powered: no\n')
        down = subprocess.CompletedProcess([], 0, 'BD Address: 00:00:00:00:00:01\n DOWN\n')
        with patch.object(bt, 'SYS', self.sys), patch.object(bt, 'checked', return_value=down), \
             patch.object(bt, 'command', side_effect=[active, off]), patch.object(bt, 'fault_logged') as logs:
            self.assertEqual(bt.assess()[0], 'skip')
            logs.assert_not_called()

    def test_usb_controller_not_reset(self):
        self.setup_sysfs(uart=False)
        with patch.object(bt, 'SYS', self.sys), \
             patch.object(bt, 'command', return_value=subprocess.CompletedProcess([], 0, 'active\n')):
            self.assertEqual(bt.assess()[0], 'skip')

    def test_known_fault_required_for_down_controller(self):
        self.setup_sysfs()
        down = subprocess.CompletedProcess([], 0, 'BD Address: 00:00:00:00:00:01\n DOWN\n')
        def cmd(args, **kwargs):
            if args[0] == 'systemctl':
                return subprocess.CompletedProcess([], 0, 'active\n')
            return subprocess.CompletedProcess([], 1, '')
        with patch.object(bt, 'SYS', self.sys), patch.object(bt, 'command', side_effect=cmd), \
             patch.object(bt, 'checked', return_value=down), patch.object(bt, 'fault_logged', return_value=False):
            self.assertEqual(bt.assess()[0], 'unknown')

    def test_missing_uart_with_known_fault_can_recover(self):
        self.setup_sysfs()
        (self.sys / 'class/bluetooth/hci0').unlink()
        with patch.object(bt, 'SYS', self.sys), \
             patch.object(bt, 'command', return_value=subprocess.CompletedProcess([], 0, 'active\n')), \
             patch.object(bt, 'fault_logged', return_value=True):
            self.assertEqual(bt.assess()[0], 'fault')

    def test_turning_off_between_attempts_stops_recovery(self):
        inspect = Mock(side_effect=[('fault', 'test'), ('skip', 'radio off')])
        reset = Mock()
        self.assertEqual(bt.recover(self.state, 'test-boot', inspect, reset, Mock()), 0)
        reset.assert_called_once()

    def test_ready_controller_after_reload_does_not_restart_daemon(self):
        self.setup_sysfs()
        with patch.object(bt, 'SYS', self.sys), patch.object(bt, 'checked') as cmd, \
             patch.object(bt, 'command', return_value=subprocess.CompletedProcess([], 0, 'active\n')), \
             patch.object(bt, 'assess', return_value=('healthy', 'test')):
            bt.reload_uart()
        self.assertNotIn(['systemctl', 'restart', 'bluetooth.service'], [call.args[0] for call in cmd.call_args_list])

    def test_no_journal_match_and_permission_failure_are_distinct(self):
        empty = subprocess.CompletedProcess([], 1, '', '')
        with patch.object(bt, 'command', return_value=empty):
            self.assertFalse(bt.fault_logged())
        denied = subprocess.CompletedProcess([], 1, '', 'Permission denied')
        with patch.object(bt, 'command', return_value=denied):
            with self.assertRaises(RuntimeError):
                bt.fault_logged()


if __name__ == '__main__':
    unittest.main()
