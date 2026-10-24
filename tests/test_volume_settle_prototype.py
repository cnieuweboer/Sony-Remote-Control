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
        self.power = 'active'
    def get_power(self):
        return self.power
    def get_volume(self):
        return dict(target='speaker', volume=next(self.readings), minVolume=0, maxVolume=100, mute=False)
    def adjust_volume(self, direction):
        self.commands.append(direction)


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
    def run_test(self, readings, target=21, correction=False, **kwargs):
        api = Api(readings)
        result = R['run_experiment'](api, target, 500, 500, correction, self.cancel, self.logs.append,
            clock=lambda: self.now, wait=kwargs.get('wait', self.wait), observed=kwargs.get('observed'))
        return api, result

    def test_target_retained_requires_no_correction(self):
        api, result = self.run_test([20, 21, 21], correction=True)
        self.assertEqual(result, 'retained')
        self.assertEqual(api.commands, [1])
        self.assertEqual(self.waits, [.5, .5])

    def test_one_step_rollback_is_logged_without_correction_by_default(self):
        api, result = self.run_test([20, 21, 20])
        self.assertEqual(result, 'short')
        self.assertEqual(api.commands, [1])
        self.assertTrue(any('Correction disabled' in line for line in self.logs))

    def test_one_correction_with_separate_verification(self):
        api, result = self.run_test([20, 21, 20, 21], correction=True)
        self.assertEqual(result, 'corrected')
        self.assertEqual(api.commands, [1, 1])
        self.assertEqual(self.waits, [.5, .5, .5])

    def test_correction_does_not_repeat_if_rollback_happens_again(self):
        api, result = self.run_test([20, 21, 20, 20], correction=True)
        self.assertEqual(result, 'correction_unconfirmed')
        self.assertEqual(api.commands, [1, 1])

    def test_downward_rollback_uses_original_direction(self):
        api, result = self.run_test([20, 19, 20, 19], target=19, correction=True)
        self.assertEqual(result, 'corrected')
        self.assertEqual(api.commands, [-1, -1])

    def test_larger_difference_and_overshoot_are_not_corrected(self):
        for actual in (18, 22):
            with self.subTest(actual=actual):
                api, result = self.run_test([20, 21, actual], correction=True)
                self.assertEqual(result, 'different')
                self.assertEqual(api.commands, [1])

    def test_target_already_reached_does_not_guess_direction(self):
        api, result = self.run_test([21, 20], correction=True)
        self.assertEqual(result, 'different')
        self.assertEqual(api.commands, [])

    def test_initial_crossing_stops_without_reversing_or_settling(self):
        api, result = self.run_test([20, 22], correction=True)
        self.assertEqual(result, 'crossed')
        self.assertEqual(api.commands, [1])
        self.assertEqual(self.waits, [.5])

    def test_no_progress_stops_after_five_seconds(self):
        api, result = self.run_test([20] * 11)
        self.assertEqual(result, 'stalled')
        self.assertEqual(api.commands, [1] * 10)
        self.assertEqual(self.now, 5)

    def test_stop_during_settling_never_sends_correction(self):
        api = Api([20, 21])
        def wait(seconds):
            self.wait(seconds)
            if len(self.waits) == 2:
                self.cancel.set()
            return self.cancel.is_set()
        with self.assertRaises(R['Cancelled']):
            R['run_experiment'](api, 21, 500, 500, True, self.cancel, self.logs.append,
                clock=lambda: self.now, wait=wait)
        self.assertEqual(api.commands, [1])

    def test_error_during_command_is_not_retried(self):
        api = Api([20])
        api.adjust_volume = Mock(side_effect=TimeoutError('uncertain result'))
        with self.assertRaises(TimeoutError):
            R['run_experiment'](api, 21, 500, 500, True, self.cancel, self.logs.append,
                clock=lambda: self.now, wait=self.wait)
        api.adjust_volume.assert_called_once_with(1)

    def test_standby_and_out_of_range_target_do_not_send_commands(self):
        for power, target in (('standby', 21), ('active', 101)):
            api = Api([20])
            api.power = power
            with self.subTest(power=power, target=target), self.assertRaises(ValueError):
                R['run_experiment'](api, target, 500, 500, False, self.cancel, self.logs.append,
                    clock=lambda: self.now, wait=self.wait)
            self.assertEqual(api.commands, [])

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
        api = Api([20, 20, 21, 20])
        original = api.get_volume
        def read():
            self.window.log('FAKE HTTP read from worker')
            return original()
        api.get_volume = read
        self.window.api = api
        self.assertIsNone(self.window.worker)
        self.assertFalse(api.commands)
        self.assertFalse(self.window.correction.isChecked())
        self.window.launch(None)
        self.pump_until(lambda: self.window.worker is None)
        self.assertEqual(self.window.target.value(), 20)
        self.window.target.setValue(21)
        self.window.rate.setValue(1)
        self.window.delay.setValue(1)
        self.window.start_test()
        self.pump_until(lambda: self.window.worker is None)
        self.assertEqual(api.commands, [1])
        self.assertTrue(any('one step short' in line for line in self.window.history))
        self.assertTrue(any('FAKE HTTP' in line for line in self.window.history))
        self.assertTrue(self.window.start.isEnabled())
        self.assertFalse(self.window.stop.isEnabled())

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
