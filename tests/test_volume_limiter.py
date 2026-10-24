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
            config={'VolumeRateLimit': {'Active': True, 'RateMs': 500}, 'VolumeControls': True},
            last_volume=info, _volume_observed_at=0, _status_observed_at=90,
            _status_sequence=0, calibration_server=None, volume_value=SimpleNamespace(setText=Mock()),
            update_volume_ui=Mock(), _schedule_volume_rate_step=Mock(),
            handle_request_error=Mock(), poll_status=Mock(),
            api=SimpleNamespace(get_volume=self.get_volume, adjust_volume=self.adjust_device),
            run_command=self.enqueue,
        )
        self.window.polling = SimpleNamespace(observed=Mock())
        self.window._publish_native_volume_state = Mock()
        self.window._accept_volume_status = lambda info, at: CLS._accept_volume_status(self.window, info, at)
        self.window._request_poll = lambda delay, condition=None, groups=None: REMOTE['QTimer'].singleShot(
            delay, self.window.poll_status if condition is None else
            lambda: self.window.poll_status() if condition() else None)
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
        self.window.update_volume_ui.assert_called_with(self.window.last_volume)
        self.finish_target()
        self.assertEqual(self.volume, 20 + amount)
        self.assertEqual(self.commands, [amount])

    def test_route_changes_during_read_prevent_old_relative_write(self):
        self.window._desired_volume = 21
        def read_after_handover():
            self.window._volume_route_epoch = 1
            self.window._desired_volume = 25  # A newer TV target after returning.
            return dict(self.window.last_volume, volume=20)
        self.window.api.get_volume = read_after_handover
        self.pending_result()()
        self.assertEqual(self.commands, [])
        self.assertEqual(self.window._desired_volume, 25)
        self.window._schedule_volume_rate_step.assert_called_with(0)

    def test_old_failed_step_does_not_clear_new_target_after_handover(self):
        CLS._volume_rate_step(self.window)
        _, callbacks = self.work.popleft()
        self.window._volume_route_epoch = 1
        self.window._desired_volume = 25
        callbacks['on_error'](TimeoutError())
        self.assertEqual(self.window._desired_volume, 25)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.window._schedule_volume_rate_step.assert_called_with(0)

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

    def test_limiter_read_updates_volume_freshness_without_refreshing_power(self):
        self.window._desired_volume = 21
        deliver = self.pending_result()
        deliver()
        self.assertGreater(self.window._volume_observed_at, 0)
        self.assertEqual(self.window._status_observed_at, 90)
        self.assertEqual(self.window._status_sequence, 1)
        self.window.update_volume_ui.assert_called_with(self.window.last_volume)

    def test_older_limiter_result_cannot_replace_a_newer_volume_observation(self):
        deliver = self.pending_result()
        newer = dict(self.window.last_volume, volume=30, mute=True)
        self.window._accept_volume_status(newer, REMOTE['time'].monotonic() + 1)
        deliver()
        self.assertEqual(self.window.last_volume, newer)
        self.window._schedule_volume_rate_step.assert_called_once()
        self.assertTrue(self.window._volume_rate_loop_running)

    def test_cancelled_limiter_result_is_ignored_and_does_not_resume_loop(self):
        deliver = self.pending_result()
        self.window._volume_rate_loop_running = False
        self.window._desired_volume = None
        previous = self.window.last_volume
        deliver()
        self.assertIs(self.window.last_volume, previous)
        self.window._schedule_volume_rate_step.assert_not_called()
        self.window.update_volume_ui.assert_not_called()
        self.assertFalse(self.window._volume_rate_step_running)

    def test_limiter_failure_releases_volume_poll_ownership(self):
        CLS._volume_rate_step(self.window)
        _, callbacks = self.work.popleft()
        callbacks['on_error'](RuntimeError('device failure'))
        self.assertFalse(self.window._volume_rate_loop_running)
        self.assertFalse(self.window._volume_rate_step_running)
        self.assertIsNone(self.window._desired_volume)
        self.refresh.assert_called_once_with(200, self.window.poll_status)

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
        self.window._request_poll = lambda delay, condition=None, groups=None: REMOTE['QTimer'].singleShot(
            delay, self.window.poll_status if condition is None else
            lambda: self.window.poll_status() if condition() else None)
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
