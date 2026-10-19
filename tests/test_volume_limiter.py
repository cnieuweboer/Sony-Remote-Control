"""Force volume keypresses between device work and GUI result delivery."""
from collections import deque
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'SonyRemote.pyw'), run_name='volume_limiter_tests')
CLS = REMOTE['SonyRemote']


class VolumeLimiterTests(unittest.TestCase):
    def setUp(self):
        self.volume = 20
        self.commands = []
        self.work = deque()
        self.ignore_next = False
        self.routed = False
        info = {'volume': 20, 'mute': False, 'minVolume': 0, 'maxVolume': 100}
        self.window = SimpleNamespace(
            _shutting_down=False, _volume_rate_loop_running=True,
            _volume_rate_step_running=False, _desired_volume=20, _volume_generation=0,
            _use_amplifier=lambda: self.routed, _calibration_blocked=lambda: False,
            _route_volume_command=lambda command: False,
            config={'VolumeRateLimit': {'Active': True, 'RateMs': 500}},
            last_volume=info, volume_value=SimpleNamespace(setText=Mock()),
            update_volume_ui=Mock(), _schedule_volume_rate_step=Mock(),
            handle_request_error=Mock(), poll_status=Mock(),
            api=SimpleNamespace(get_volume=self.get_volume, adjust_volume=self.adjust_device),
            run_command=self.enqueue,
        )
        self.timer = patch.object(REMOTE['QTimer'], 'singleShot')
        self.refresh = self.timer.start()
        self.addCleanup(self.timer.stop)

    def get_volume(self):
        return dict(self.window.last_volume, volume=self.volume)

    def adjust_device(self, direction):
        self.commands.append(direction)
        if self.ignore_next:
            self.ignore_next = False
        else:
            self.volume += direction

    def enqueue(self, function, **callbacks):
        self.work.append((function, callbacks))
        return True

    def pending_result(self):
        CLS._volume_rate_step(self.window)
        function, callbacks = self.work.popleft()
        result = function()
        return lambda: callbacks['on_result'](result)

    def keypress(self, amount):
        CLS.adjust_volume(self.window, amount)

    def finish_target(self):
        for _ in range(10):
            if not self.window._volume_rate_loop_running:
                return
            self.pending_result()()
        self.fail('Limiter did not finish within the fake-device step budget')

    def assert_new_key_survives(self, amount):
        deliver = self.pending_result()  # Worker sees target 20 reached.
        self.keypress(amount)  # Native key arrives before GUI completion.
        deliver()
        self.assertEqual(self.window._desired_volume, 20 + amount)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.assertEqual(self.window.volume_value.setText.call_args.args, (str(20 + amount),))
        self.window.update_volume_ui.assert_not_called()
        self.finish_target()
        self.assertEqual(self.volume, 20 + amount)
        self.assertEqual(self.commands, [amount])

    def test_new_up_key_after_reached_target_survives_old_completion(self):
        self.assert_new_key_survives(1)

    def test_new_down_key_after_reached_target_survives_old_completion(self):
        self.assert_new_key_survives(-1)

    def test_multiple_new_keys_retain_the_latest_target(self):
        deliver = self.pending_result()
        for amount in (1, 1, 1, -1):
            self.keypress(amount)
        deliver()
        self.assertEqual(self.window._desired_volume, 22)
        self.finish_target()
        self.assertEqual(self.volume, 22)
        self.assertEqual(self.commands, [1, 1])

    def test_new_key_during_a_step_that_sent_a_command_is_retained(self):
        self.window._desired_volume = 21
        deliver = self.pending_result()  # Device moved, info is from before the command.
        self.keypress(1)
        deliver()
        self.assertEqual(self.window._desired_volume, 22)
        self.finish_target()
        self.assertEqual(self.volume, 22)
        self.assertEqual(self.commands, [1, 1])

    def test_keys_returning_to_current_volume_need_no_device_command(self):
        deliver = self.pending_result()
        self.keypress(1)
        self.keypress(-1)
        deliver()
        self.assertFalse(self.window._volume_rate_loop_running)
        self.assertIsNone(self.window._desired_volume)
        self.assertFalse(self.commands)
        self.refresh.assert_called_once_with(200, self.window.poll_status)

    def test_unchanged_device_volume_is_retried_until_target_is_observed(self):
        deliver = self.pending_result()
        self.keypress(1)
        deliver()
        self.ignore_next = True
        self.finish_target()
        self.assertEqual(self.volume, 21)
        self.assertEqual(self.commands, [1, 1])

    def test_routing_change_does_not_restart_cancelled_tv_loop(self):
        deliver = self.pending_result()
        self.routed = True
        self.window._desired_volume = None
        self.window._volume_rate_loop_running = False
        deliver()
        self.assertFalse(self.window._volume_rate_step_running)
        self.window._schedule_volume_rate_step.assert_not_called()
        self.window.update_volume_ui.assert_not_called()
        self.assertFalse(self.commands)


class UnrestrictedVolumePollingTests(unittest.TestCase):
    def setUp(self):
        self.callbacks = []
        self.window = SimpleNamespace(
            _shutting_down=False, _amplifier_routed=False,
            _pending_volume_commands=0, _volume_generation=0,
            _volume_rate_loop_running=False, _desired_volume=None,
            _calibration_blocked=lambda: False, _route_volume_command=lambda command: False,
            config={'VolumeRateLimit': {'Active': False}},
            last_volume={'volume': 20, 'minVolume': 0, 'maxVolume': 100},
            volume_value=SimpleNamespace(setText=Mock()), update_volume_ui=Mock(),
            poll_status=Mock(), api=SimpleNamespace(adjust_volume=Mock()),
            run_command=self.enqueue,
        )
        timer = patch.object(REMOTE['QTimer'], 'singleShot')
        self.refresh = timer.start()
        self.addCleanup(timer.stop)

    def enqueue(self, function, **callbacks):
        self.callbacks.append(callbacks['on_finished'])
        return True

    def press(self):
        CLS.adjust_volume(self.window, 1)

    def test_only_final_queued_completion_schedules_reconciliation(self):
        for _ in range(3):
            self.press()
        for complete in self.callbacks[:2]:
            complete()
            self.refresh.assert_not_called()
        self.callbacks[2]()
        self.refresh.assert_called_once()
        delay, refresh = self.refresh.call_args.args
        self.assertEqual(delay, 200)
        refresh()
        self.window.poll_status.assert_called_once_with()
        self.assertEqual(self.window._desired_volume, 23)

    def test_new_pending_press_suppresses_delayed_poll(self):
        self.press()
        self.callbacks[0]()
        stale_refresh = self.refresh.call_args.args[1]
        self.press()
        stale_refresh()
        self.window.poll_status.assert_not_called()
        self.callbacks[1]()
        self.refresh.call_args.args[1]()
        self.window.poll_status.assert_called_once_with()

    def test_new_press_already_finished_still_invalidates_old_poll(self):
        self.press()
        self.callbacks[0]()
        stale_refresh = self.refresh.call_args.args[1]
        self.press()
        self.callbacks[1]()
        self.assertEqual(self.window._pending_volume_commands, 0)
        stale_refresh()
        self.window.poll_status.assert_not_called()
        self.refresh.call_args.args[1]()
        self.window.poll_status.assert_called_once_with()

    def test_active_rate_limiter_suppresses_delayed_poll(self):
        self.press()
        self.callbacks[0]()
        self.window._volume_rate_loop_running = True
        self.refresh.call_args.args[1]()
        self.window.poll_status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
