"""Drive real limiter callbacks with deterministic device responses and GUI races."""
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
        self.now = 100.0
        self.volume = 20
        self.commands = []
        self.events = []
        self.work = deque()
        self.readings = deque()
        self.mode = 'direct'
        self.stuck = False
        self.echo = None
        self.routed = False
        self.blocked = False
        info = {'volume': 20, 'mute': False, 'minVolume': 0, 'maxVolume': 100}
        self.window = SimpleNamespace(
            _shutting_down=False, last_power='active', _standby_confirmation_after=None,
            _volume_rate_loop_running=True, _volume_rate_step_running=False,
            _volume_rate_progress=None, _volume_rate_phase='read',
            _volume_rate_candidate=None, _volume_rate_due=0.0,
            _desired_volume=20, _volume_generation=0, _volume_route_epoch=0,
            _use_amplifier=lambda: self.routed, _calibration_blocked=lambda: self.blocked,
            _route_volume_command=lambda command: False,
            config={'VolumeRateLimit': {'Active': True, 'RateMs': 600, 'VerificationDelayMs': 500},
                    'VolumeControls': True},
            last_volume=info, _volume_observed_at=0, _status_observed_at=90,
            _status_sequence=0, calibration_server=None, volume_value=SimpleNamespace(setText=Mock()),
            volume_rate_timer=Mock(), update_volume_ui=Mock(), _schedule_volume_rate_step=Mock(),
            handle_request_error=Mock(), poll_status=Mock(),
            api=SimpleNamespace(get_volume=self.get_volume, set_volume=self.set_volume),
            run_command=self.enqueue,
        )
        self.window.polling = SimpleNamespace(observed=Mock())
        self.window._publish_native_volume_state = Mock()
        self.window._accept_volume_status = lambda info, at: CLS._accept_volume_status(self.window, info, at)
        self.window._tv_controls_available = lambda: CLS._tv_controls_available(self.window)
        self.window._request_poll = Mock()
        timer = patch.object(REMOTE['time'], 'monotonic', lambda: self.now)
        timer.start()
        self.addCleanup(timer.stop)

    def get_volume(self):
        if self.readings:
            volume = self.readings.popleft()
        elif self.echo is not None:
            volume, self.echo = self.echo, None
        else:
            volume = self.volume
        self.events.append(('read', self.now, volume))
        return dict(self.window.last_volume, volume=volume)

    def set_volume(self, target):
        self.commands.append(target)
        self.events.append(('command', self.now, target))
        if not self.stuck:
            if self.mode == 'direct':
                self.volume = target
            else:
                direction = 1 if target > self.volume else -1
                self.volume += direction * (2 if self.mode == 'overshoot' else 1)
        if self.mode == 'echo':
            self.echo = target

    def enqueue(self, function, **callbacks):
        self.work.append((function, callbacks))
        return True

    def pending_result(self, advance=True):
        if advance:
            self.now = max(self.now, self.window._volume_rate_due)
        CLS._volume_rate_step(self.window)
        function, callbacks = self.work.popleft()
        result = function()
        return lambda: callbacks['on_result'](result)

    def keypress(self, amount):
        CLS.adjust_volume(self.window, amount)

    def finish_target(self):
        for _ in range(80):
            if not self.window._volume_rate_loop_running:
                return
            self.pending_result()()
        self.fail('Limiter failed to finish within fake-device budget')

    def test_direct_speakers_need_one_absolute_command_and_two_delayed_reads(self):
        self.keypress(10)
        self.finish_target()
        self.assertEqual(self.commands, [30])
        self.assertEqual([event[0] for event in self.events], ['read', 'command', 'read', 'read'])
        self.assertAlmostEqual(self.events[2][1] - self.events[1][1], .6)
        self.assertAlmostEqual(self.events[3][1] - self.events[2][1], .5)
        self.assertIsNone(self.window._desired_volume)
        self.window._request_poll.assert_called_once_with(200, groups={'volume'})

    def test_no_read_or_command_can_run_early_during_quiet_period(self):
        self.keypress(1)
        self.pending_result()()
        before = list(self.events)
        self.now += .3
        self.pending_result(advance=False)()
        self.assertEqual(self.events, before)
        self.assertGreater(self.window._volume_rate_due, self.now)
        self.finish_target()
        self.assertEqual(self.commands, [21])

    def test_quiet_period_starts_after_http_completion_not_queueing(self):
        original = self.window.api.set_volume
        def slow(target):
            original(target)
            self.now += 2
        self.window.api.set_volume = slow
        self.keypress(1)
        self.pending_result()()
        self.assertAlmostEqual(self.window._volume_rate_due, 102.6)
        self.finish_target()
        self.assertAlmostEqual(self.events[2][1], 102.6)

    def test_cec_target_echoes_then_rollback_continue_in_both_directions(self):
        self.mode = 'echo'
        self.keypress(5)
        self.finish_target()
        self.assertEqual(self.commands, [25] * 5)
        self.assertEqual(self.volume, 25)
        self.window._volume_rate_loop_running = False
        self.keypress(-3)
        self.finish_target()
        self.assertEqual(self.commands[-3:], [22] * 3)
        self.assertEqual(self.volume, 22)

    def test_provisional_target_echoes_do_not_reset_no_progress_timeout(self):
        self.mode = 'echo'
        self.stuck = True
        self.keypress(10)
        self.finish_target()
        self.assertFalse(self.window._volume_rate_loop_running)
        self.assertGreaterEqual(self.now, 105)
        self.assertLess(self.now, 107)
        self.assertLessEqual(len(self.commands), 6)
        self.assertEqual(self.window.last_volume['volume'], 20)
        self.assertIsNone(self.window._desired_volume)

    def test_no_movement_without_echoes_also_stops(self):
        self.stuck = True
        self.keypress(5)
        self.finish_target()
        self.assertLessEqual(len(self.commands), 10)
        self.assertEqual(self.volume, 20)
        self.assertIsNone(self.window._desired_volume)

    def test_real_progress_can_take_longer_than_five_seconds(self):
        self.mode = 'echo'
        self.keypress(10)
        self.finish_target()
        self.assertEqual(self.volume, 30)
        self.assertEqual(self.commands, [30] * 10)
        self.assertGreater(self.now, 110)

    def test_crossing_stops_without_sending_reverse_command(self):
        for amount in (1, -1):
            with self.subTest(amount=amount):
                self.volume = 20
                self.window.last_volume['volume'] = 20
                self.window._volume_rate_loop_running = False
                self.commands.clear()
                self.mode = 'overshoot'
                self.keypress(amount)
                self.finish_target()
                self.assertEqual(self.commands, [20 + amount])
                self.assertEqual(self.window.last_volume['volume'], 20 + 2 * amount)

    def test_new_key_after_provisional_result_is_not_cleared(self):
        deliver = self.pending_result()  # Existing volume 20 matches old target 20.
        self.keypress(1)
        deliver()
        self.assertEqual(self.window._desired_volume, 21)
        self.finish_target()
        self.assertEqual(self.commands, [21])

    def test_new_key_during_quiet_does_not_bypass_it(self):
        self.keypress(1)
        self.pending_result()()
        self.keypress(1)
        before = list(self.events)
        self.pending_result(advance=False)()
        self.assertEqual(self.events, before)
        self.finish_target()
        self.assertEqual(self.commands, [21, 22])

    def test_old_verification_cannot_finish_newer_target(self):
        self.keypress(1)
        self.pending_result()()  # Send 21.
        self.pending_result()()  # First report of 21.
        deliver = self.pending_result()  # Confirms 21; delivery delayed.
        self.keypress(1)
        deliver()
        self.assertEqual(self.window._desired_volume, 22)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.finish_target()
        self.assertEqual(self.commands, [21, 22])

    def test_new_key_while_command_in_flight_preserves_quiet_and_new_target(self):
        original = self.window.api.set_volume
        def command(target):
            original(target)
            self.keypress(1)
        self.window.api.set_volume = command
        self.keypress(1)
        self.pending_result()()
        self.window.api.set_volume = original
        self.assertEqual(self.window._desired_volume, 22)
        self.assertAlmostEqual(self.window._volume_rate_due, 100.6)
        self.finish_target()
        self.assertEqual(self.commands, [21, 22])

    def test_reduced_target_does_not_take_direction_from_old_requested_value_echo(self):
        self.mode = 'echo'
        self.keypress(10)  # Requested 30, actual CEC volume moves only to 21.
        self.pending_result()()
        self.keypress(-1)  # New target 29 is still above actual volume.
        self.pending_result()()  # Old request echo says 30; do not infer "down".
        self.assertEqual(self.commands, [30])
        self.assertEqual(self.window._volume_rate_phase, 'verify')
        self.finish_target()
        self.assertEqual(self.volume, 29)
        self.assertEqual(self.commands, [30] + [29] * 8)

    def test_new_key_after_stalled_worker_result_is_not_discarded(self):
        self.stuck = True
        self.keypress(1)
        self.pending_result()()
        self.now = 106
        deliver = self.pending_result()
        self.keypress(1)
        deliver()
        self.assertEqual(self.window._desired_volume, 22)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.stuck = False
        self.finish_target()
        self.assertEqual(self.volume, 22)

    def test_new_target_cancels_old_queued_write(self):
        self.keypress(1)
        CLS._volume_rate_step(self.window)
        function, callbacks = self.work.popleft()
        self.keypress(1)
        callbacks['on_result'](function())
        self.assertFalse(self.commands)
        self.finish_target()
        self.assertEqual(self.commands, [22])

    def test_routing_change_during_read_prevents_write(self):
        def read():
            self.window._volume_route_epoch += 1
            self.window._desired_volume = 25
            return dict(self.window.last_volume)
        self.window.api.get_volume = read
        self.keypress(1)
        self.pending_result()()
        self.assertFalse(self.commands)
        self.assertEqual(self.window._desired_volume, 25)

    def test_new_loop_after_routing_change_honors_old_inflight_command_quiet(self):
        original = self.window.api.set_volume
        def command(target):
            original(target)
            self.window._volume_route_epoch += 1
            self.window._volume_generation += 1
            self.window._desired_volume = 25
        self.window.api.set_volume = command
        self.keypress(1)
        self.pending_result()()
        self.assertAlmostEqual(self.window._volume_rate_due, 100.6)
        self.assertEqual(self.window._desired_volume, 25)

    def test_cancellation_during_read_prevents_write_and_rescheduling(self):
        def read():
            self.window._volume_rate_loop_running = False
            self.window._desired_volume = None
            return dict(self.window.last_volume)
        self.window.api.get_volume = read
        self.keypress(1)
        self.window._schedule_volume_rate_step.reset_mock()
        self.pending_result()()
        self.assertFalse(self.commands)
        self.window._schedule_volume_rate_step.assert_not_called()

    def test_calibration_or_standby_cancels_limiter(self):
        for kind in ('calibration', 'standby'):
            with self.subTest(kind=kind):
                self.window._volume_rate_loop_running = True
                self.window._desired_volume = 21
                self.blocked = kind == 'calibration'
                self.window.last_power = 'standby' if kind == 'standby' else 'active'
                CLS._volume_rate_step(self.window)
                self.assertFalse(self.work)
                self.assertFalse(self.window._volume_rate_loop_running)
                self.assertIsNone(self.window._desired_volume)

    def test_new_target_survives_old_worker_failure(self):
        self.keypress(1)
        CLS._volume_rate_step(self.window)
        _, callbacks = self.work.popleft()
        self.keypress(1)
        callbacks['on_error'](TimeoutError())
        self.assertEqual(self.window._desired_volume, 22)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.assertAlmostEqual(self.window._volume_rate_due, 100.6)

    def test_failure_for_current_target_stops_and_refreshes(self):
        CLS._volume_rate_step(self.window)
        _, callbacks = self.work.popleft()
        callbacks['on_error'](TimeoutError())
        self.assertFalse(self.window._volume_rate_loop_running)
        self.assertIsNone(self.window._desired_volume)
        self.window.handle_request_error.assert_called_once()
        self.window._request_poll.assert_called_once_with(200, groups={'volume'})

    def test_reads_update_mute_and_freshness_without_refreshing_power(self):
        self.window.last_volume['mute'] = True
        self.keypress(1)
        self.pending_result()()
        self.assertTrue(self.window.last_volume['mute'])
        self.assertEqual(self.window._volume_observed_at, 100)
        self.assertEqual(self.window._status_observed_at, 90)
        self.window.polling.observed.assert_called_with('volume', 100)

    def test_newer_volume_observation_prevents_old_confirmation_from_finishing(self):
        self.keypress(1)
        self.pending_result()()
        self.pending_result()()
        deliver = self.pending_result()  # Verification observed 21.
        newer = dict(self.window.last_volume, volume=20, mute=True)
        self.window._accept_volume_status(newer, self.now + .1)
        deliver()
        self.assertEqual(self.window.last_volume, newer)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.assertEqual(self.window._desired_volume, 21)
        self.assertGreater(self.window._volume_rate_due, self.now)
        self.finish_target()

    def test_late_failure_after_cancellation_does_not_show_error_or_resume(self):
        CLS._volume_rate_step(self.window)
        _, callbacks = self.work.popleft()
        self.window._volume_rate_loop_running = False
        self.window._desired_volume = None
        callbacks['on_error'](TimeoutError())
        self.window.handle_request_error.assert_not_called()
        self.window._schedule_volume_rate_step.assert_not_called()

    def test_validation_and_defaults(self):
        validate = REMOTE['validate_config']
        self.assertEqual(validate({}, allow_incomplete=True)['VolumeRateLimit'],
                         {'Active': True, 'RateMs': 600, 'VerificationDelayMs': 500})
        for value in (None, True, 0, -1, 0.5, '500'):
            with self.subTest(value=value), self.assertRaises(REMOTE['ConfigError']):
                validate({'VolumeRateLimit': {'VerificationDelayMs': value}}, allow_incomplete=True)


class UnrestrictedVolumePollingTests(unittest.TestCase):
    def setUp(self):
        self.callbacks = []
        self.window = SimpleNamespace(
            _shutting_down=False, _amplifier_routed=False, last_power='active', _standby_confirmation_after=None,
            _pending_volume_commands=0, _volume_generation=0, _volume_route_epoch=0,
            _volume_rate_loop_running=False, _desired_volume=None,
            _calibration_blocked=lambda: False, _route_volume_command=lambda command: False,
            config={'VolumeRateLimit': {'Active': False}},
            last_volume={'volume': 20, 'minVolume': 0, 'maxVolume': 100},
            volume_value=SimpleNamespace(setText=Mock()), update_volume_ui=Mock(),
            poll_status=Mock(), api=SimpleNamespace(adjust_volume=Mock()),
            run_command=self.enqueue,
        )
        self.window._tv_controls_available = lambda: CLS._tv_controls_available(self.window)
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

    def test_cancelled_relative_commands_do_not_send_or_schedule_old_refresh(self):
        jobs = []
        def enqueue(function, **callbacks):
            jobs.append((function, callbacks))
            return True
        self.window.run_command = enqueue
        self.press()
        self.press()
        self.window._volume_route_epoch = 1  # App standby invalidates previous volume work.
        self.window._desired_volume = None
        for function, callbacks in jobs:
            function()
            callbacks['on_finished']()
        self.window.api.adjust_volume.assert_not_called()
        self.assertEqual(self.window._pending_volume_commands, 0)
        self.refresh.assert_not_called()

    def test_active_rate_limiter_suppresses_delayed_poll(self):
        self.press()
        self.callbacks[0]()
        self.window._volume_rate_loop_running = True
        self.refresh.call_args.args[1]()
        self.window.poll_status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
