"""Exercise settling/rollback cases without real devices or elapsed waits."""
import json
from pathlib import Path
import runpy
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from PySide6.QtWidgets import QApplication

R = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'prototypes' / 'TVVolumeSettlePrototype.pyw'),
                  run_name='volume_settle_prototype_tests')


class Api:
    def __init__(self, readings):
        self.readings = iter(readings)
        self.commands = []
        self.absolute_commands = []
        self.power = 'active'
    def get_power(self):
        return self.power
    def get_volume(self):
        return dict(target='speaker', volume=next(self.readings), minVolume=0, maxVolume=100, mute=False)
    def adjust_volume(self, direction):
        self.commands.append(direction)
    def set_volume(self, target):
        self.absolute_commands.append(target)


class Tests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.waits = []
        self.cancel = threading.Event()
        self.logs = []
    def wait(self, seconds):
        self.now += seconds
        self.waits.append(seconds)
        return self.cancel.is_set()
    def run_test(self, readings, target=21, **kwargs):
        api = Api(readings)
        result = R['run_experiment'](api, target, 500, 500, self.cancel, self.logs.append,
            absolute=kwargs.get('absolute', False), clock=lambda: self.now,
            wait=kwargs.get('wait', self.wait), observed=kwargs.get('observed'))
        return api, result

    def test_target_retained_after_one_send(self):
        api, result = self.run_test([20, 21, 21])
        self.assertEqual(result, 'retained')
        self.assertEqual(api.commands, [1])
        self.assertEqual(self.waits, [.5, .5])

    def test_one_step_rollback_resumes_and_reverifies(self):
        api, result = self.run_test([20, 21, 20, 21, 21])
        self.assertEqual(result, 'retained')
        self.assertEqual(api.commands, [1, 1])
        self.assertEqual(self.waits, [.5] * 4)
        self.assertTrue(any('TARGET NOT SETTLED' in line for line in self.logs))

    def test_two_step_rollback_is_not_restricted_to_one_correction(self):
        api, result = self.run_test([30, 31, 32, 33, 34, 32, 33, 34, 34], target=34)
        self.assertEqual(result, 'retained')
        self.assertEqual(api.commands, [1] * 6)

    def test_absolute_cec_requested_target_echoes_do_not_end_test_early(self):
        for start, target, actuals in ((32, 26, [31, 30, 29, 28, 27, 26]),
                                       (24, 30, [25, 26, 27, 28, 29, 30])):
            readings = [start]
            for actual in actuals:
                readings.extend([target, actual])
            with self.subTest(start=start, target=target):
                api, result = self.run_test(readings, target=target, absolute=True)
                self.assertEqual(result, 'retained')
                self.assertEqual(api.absolute_commands, [target] * 6)
                self.assertFalse(api.commands)

    def test_transient_target_echoes_without_progress_still_time_out(self):
        api, result = self.run_test([32] + [26, 32] * 5, target=26, absolute=True)
        self.assertEqual(result, 'stalled')
        self.assertEqual(api.absolute_commands, [26] * 5)
        self.assertEqual(self.now, 5)

    def test_returning_to_same_best_value_does_not_restart_timeout(self):
        api, result = self.run_test([32, 26, 31] + [26, 31] * 5, target=26, absolute=True)
        self.assertEqual(result, 'stalled')
        self.assertEqual(api.absolute_commands, [26] * 6)
        self.assertEqual(self.now, 6)

    def test_sustained_progress_can_take_longer_than_five_seconds(self):
        readings = [30]
        for actual in range(29, 19, -1):
            readings.extend([20, actual])
        api, result = self.run_test(readings, target=20, absolute=True)
        self.assertEqual(result, 'retained')
        self.assertEqual(len(api.absolute_commands), 10)
        self.assertEqual(self.now, 10)

    def test_relative_downward_rollback_uses_original_direction(self):
        api, result = self.run_test([20, 19, 20, 19, 19], target=19)
        self.assertEqual(result, 'retained')
        self.assertEqual(api.commands, [-1, -1])

    def test_delayed_check_crossing_stops_without_reversing(self):
        api, result = self.run_test([20, 21, 22])
        self.assertEqual(result, 'crossed')
        self.assertEqual(api.commands, [1])

    def test_initial_crossing_stops_without_extra_verification(self):
        api, result = self.run_test([20, 22])
        self.assertEqual(result, 'crossed')
        self.assertEqual(api.commands, [1])
        self.assertEqual(self.waits, [.5])

    def test_initially_at_target_does_not_guess_direction_after_rollback(self):
        api, result = self.run_test([21, 20])
        self.assertEqual(result, 'different')
        self.assertFalse(api.commands)

    def test_no_progress_without_target_echoes_stops_after_five_seconds(self):
        api, result = self.run_test([20] * 11)
        self.assertEqual(result, 'stalled')
        self.assertEqual(api.commands, [1] * 10)
        self.assertEqual(self.now, 5)

    def test_stop_during_settling_never_sends_again(self):
        api = Api([20, 21])
        def wait(seconds):
            self.wait(seconds)
            if len(self.waits) == 2:
                self.cancel.set()
            return self.cancel.is_set()
        with self.assertRaises(R['Cancelled']):
            R['run_experiment'](api, 21, 500, 500, self.cancel, self.logs.append,
                clock=lambda: self.now, wait=wait)
        self.assertEqual(api.commands, [1])

    def test_stop_after_failed_verification_prevents_resumed_write(self):
        api = Api([20, 21, 20])
        def observed(info):
            if len(self.waits) == 2:
                self.cancel.set()
        with self.assertRaises(R['Cancelled']):
            R['run_experiment'](api, 21, 500, 500, self.cancel, self.logs.append,
                clock=lambda: self.now, wait=self.wait, observed=observed)
        self.assertEqual(api.commands, [1])

    def test_error_during_command_is_not_retried(self):
        api = Api([20])
        api.adjust_volume = Mock(side_effect=TimeoutError('uncertain result'))
        with self.assertRaises(TimeoutError):
            R['run_experiment'](api, 21, 500, 500, self.cancel, self.logs.append,
                clock=lambda: self.now, wait=self.wait)
        api.adjust_volume.assert_called_once_with(1)

    def test_standby_and_out_of_range_target_do_not_send_commands(self):
        for power, target in (('standby', 21), ('active', 101)):
            api = Api([20])
            api.power = power
            with self.subTest(power=power, target=target), self.assertRaises(ValueError):
                R['run_experiment'](api, target, 500, 500, self.cancel, self.logs.append,
                    clock=lambda: self.now, wait=self.wait)
            self.assertFalse(api.commands)

    def test_absolute_direct_jump_needs_only_one_command(self):
        api, result = self.run_test([10, 20, 20], target=20, absolute=True)
        self.assertEqual(result, 'retained')
        self.assertEqual(api.absolute_commands, [20])
        self.assertFalse(api.commands)

    def test_absolute_api_payload(self):
        api = R['TvApi']({'Host': 'device.local', 'PreSharedKey': 'test-only'}, self.logs.append)
        api.rpc = Mock(return_value=[0])
        api.set_volume(20)
        api.rpc.assert_called_once_with('audio', 'setAudioVolume',
            [{'target': 'speaker', 'volume': '20', 'ui': 'on'}], version='1.2')

    def test_relative_api_payload(self):
        api = R['TvApi']({'Host': 'device.local', 'PreSharedKey': 'test-only'}, self.logs.append)
        api.rpc = Mock(return_value=[0])
        api.adjust_volume(-1)
        api.rpc.assert_called_once_with('audio', 'setAudioVolume', [{'target': 'speaker', 'volume': '-1'}])

    def test_settings_are_read_only_and_only_tv_config_is_needed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SonyRemote.json'
            text = json.dumps({'Host': 'device.local', 'PreSharedKey': 'test-only'})
            path.write_text(text)
            self.assertEqual(R['load_settings'](Path(folder)), json.loads(text))
            self.assertEqual(path.read_text(), text)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)


class WindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = R['Window']({'Host': 'device.local', 'PreSharedKey': 'test-only'})
        self.addCleanup(self.window.close)

    def pump_until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        self.assertTrue(condition(), 'Qt worker did not reach expected state')

    def test_manual_refresh_then_run_logs_rollback_and_returns_to_idle(self):
        api = Api([20, 20, 21, 20, 21, 21])
        original = api.get_volume
        def read():
            self.window.log('FAKE HTTP read from worker')
            return original()
        api.get_volume = read
        self.window.api = api
        self.assertIsNone(self.window.worker)
        self.assertFalse(api.commands)
        self.assertFalse(self.window.absolute.isChecked())
        self.window.launch(None)
        self.pump_until(lambda: self.window.worker is None)
        self.assertEqual(self.window.target.value(), 20)
        self.window.target.setValue(21)
        self.window.rate.setValue(1)
        self.window.delay.setValue(1)
        self.window.start_test()
        self.pump_until(lambda: self.window.worker is None)
        self.assertEqual(api.commands, [1, 1])
        self.assertTrue(any('TARGET NOT SETTLED' in line for line in self.window.history))
        self.assertTrue(any('FAKE HTTP' in line for line in self.window.history))
        self.assertTrue(self.window.start.isEnabled())
        self.assertFalse(self.window.stop.isEnabled())

    def test_absolute_toggle_reaches_worker_and_is_locked_during_run(self):
        api = Api([20, 21, 21])
        self.window.api = api
        self.window.absolute.setChecked(True)
        self.window.target.setValue(21)
        self.window.rate.setValue(1)
        self.window.delay.setValue(1)
        self.window.start_test()
        self.assertFalse(self.window.absolute.isEnabled())
        self.pump_until(lambda: self.window.worker is None)
        self.assertEqual(api.absolute_commands, [21])
        self.assertFalse(api.commands)
        self.assertTrue(self.window.absolute.isEnabled())
        self.assertTrue(any('"command_mode": "absolute"' in line for line in self.window.history))

    def test_absolute_worker_continues_after_repeated_target_echoes(self):
        api = Api([32, 26, 31, 26, 30, 26, 29, 26, 28, 26, 27, 26, 26])
        self.window.api = api
        self.window.absolute.setChecked(True)
        self.window.target.setValue(26)
        self.window.rate.setValue(1)
        self.window.delay.setValue(1)
        self.window.start_test()
        self.pump_until(lambda: self.window.worker is None)
        self.assertEqual(api.absolute_commands, [26] * 6)
        self.assertFalse(api.commands)
        self.assertTrue(any('RESULT: target 26 retained' in line for line in self.window.history))
        self.assertEqual(sum('TARGET NOT SETTLED' in line for line in self.window.history), 5)

    def test_stop_interrupts_worker_settle_wait(self):
        api = Api([21])
        self.window.api = api
        self.window.target.setValue(21)
        self.window.delay.setValue(2000)
        self.window.start_test()
        self.pump_until(lambda: any('TARGET FIRST REPORTED' in line for line in self.window.history))
        self.window.stop_test()
        self.pump_until(lambda: self.window.worker is None)
        self.assertFalse(api.commands)
        self.assertFalse(any('SETTLED CHECK' in line for line in self.window.history))
        self.assertTrue(any('Stopped while waiting' in line for line in self.window.history))


if __name__ == '__main__':
    unittest.main()
