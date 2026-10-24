"""Standby availability across real-window controls, shortcuts and queued work."""
import math
import threading
import unittest
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
import test_graceful_failures as fixture


class StandbyControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        self.f = fixture.DiscoveryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.start_and_settle()
        self.w = self.f.window
        self.w.activateWindow()
        self.w.power_button.setFocus()
        self.f.app.processEvents()

    def assert_controls_blocked(self):
        w = self.w
        for button in (w.volume_up, w.volume_down, w.mute_button, self.f.buttons('Up')[0]):
            self.assertFalse(button.isEnabled())
        self.assertFalse(w.app_combo.isEnabled())
        self.assertFalse(w.input_combo.isEnabled())
        self.assertFalse(w.text_input.isEnabled())
        for command in ('Up', 'volume_up', 'volume_down', 'mute', 'focus_text_input'):
            self.assertFalse(w._focused_shortcut_allowed(command, custom=True))
            self.assertFalse(w._accept_mouse_shortcut(command))
        w._publish_native_volume_state()
        for command in ('volume_up', 'volume_down', 'mute'):
            self.assertFalse(w._accept_native_volume(command, int(w.winId())))

    def test_standby_and_offline_shortcuts_never_send_to_tv_or_amplifier(self):
        w = self.w
        w.api.ircc = Mock()
        w._route_volume_command = Mock(return_value=True)
        for power in ('standby', None):
            with self.subTest(power=power):
                w.last_power = power
                w.set_controls_enabled(False, power_enabled=power is not None)
                self.assert_controls_blocked()
                for key in (Qt.Key_Up, Qt.Key_Return, Qt.Key_MediaNext,
                            Qt.Key_VolumeUp, Qt.Key_VolumeDown, Qt.Key_VolumeMute):
                    QTest.keyClick(w.power_button, key)
                w.adjust_volume(1)
                w.toggle_mute()
        w.api.ircc.assert_not_called()
        w._route_volume_command.assert_not_called()

    def test_power_button_can_still_wake_tv(self):
        self.f.device.power = self.w.last_power = 'standby'
        self.w.set_controls_enabled(False, power_enabled=True)
        self.w.power_button.click()
        self.f.wait(lambda: self.w.last_power == 'active' and not self.w.poll_running)
        self.assertTrue(self.w.volume_up.isEnabled())
        self.assertEqual(self.f.device.calls['set_power'], 1)

    def test_already_queued_native_volume_is_discarded_when_standby_starts(self):
        w = self.w
        w._publish_native_volume_state()
        self.assertTrue(w._accept_native_volume('volume_up', int(w.winId())))
        w.adjust_volume = Mock()
        w._standby_confirmation_after = math.inf
        w._drain_native_volume()
        w.adjust_volume.assert_not_called()
        w._publish_native_volume_state()
        self.assertFalse(w._accept_native_volume('volume_up', int(w.winId())))

    def test_pending_standby_blocks_new_commands_and_late_control_refreshes(self):
        w = self.w
        hold = self.f.device.holds['set_power'] = threading.Event()
        w.api.ircc = Mock()
        w.api.set_mute = Mock()
        w.api.launch_app = Mock()
        w.api.set_input = Mock()
        w.api.set_text_form = Mock()
        before = self.f.device.calls['volume']
        w.toggle_power()
        self.f.wait(lambda: self.f.device.calls['set_power'] == 1)
        self.assertEqual(w._standby_confirmation_after, math.inf)
        # Discovery and IPC updates must not revive disabled controls.
        w.set_controls_enabled(True, power_enabled=True)
        w._update_volume_controls()
        self.assert_controls_blocked()
        self.assertFalse(w.power_button.isEnabled())
        w.volume_up.click()
        w.adjust_volume(1)
        w.toggle_mute()
        w.launch_app('app')
        w.set_input('input')
        w.send_text_input()
        w.send_text_input_and_confirm()
        w.toggle_power()
        for key in (Qt.Key_Up, Qt.Key_VolumeUp):
            QTest.keyClick(w.power_button, key)
        self.assertEqual(len(w._command_workers), 1)
        self.assertFalse(w._volume_rate_loop_running)
        self.assertFalse(w.volume_rate_timer.isActive())
        hold.set()
        self.f.wait(lambda: w.last_power == 'standby' and not w.poll_running)
        self.assertEqual(self.f.device.calls['volume'], before)
        self.assertEqual(self.f.device.calls['set_volume'], 0)
        self.assertEqual(self.f.device.calls['set_power'], 1)
        for action in (w.api.ircc, w.api.set_mute, w.api.launch_app, w.api.set_input, w.api.set_text_form):
            action.assert_not_called()
        w.show_error.assert_not_called()

    def test_older_power_result_cannot_clear_pending_standby(self):
        w = self.w
        jobs = []
        with patch.object(w, 'run_worker', side_effect=lambda fn, *args, **kwargs: jobs.append((fn, args)) or True):
            w.poll_status(groups={'power'})
        query, callbacks = jobs.pop()
        old_result = query()
        w.toggle_power()
        self.f.wait(lambda: w._standby_confirmation_after is not None
                    and math.isfinite(w._standby_confirmation_after))
        # Deliver the pre-command active response after command completion.
        callbacks[0](old_result)
        self.assertEqual(w.last_power, 'active')
        self.assertIsNotNone(w._standby_confirmation_after)
        self.assert_controls_blocked()
        callbacks[2]()
        self.f.wait(lambda: w.last_power == 'standby' and not w.poll_running)
        self.assertIsNone(w._standby_confirmation_after)
        self.assertTrue(w.power_button.isEnabled())

    def test_volume_only_poll_cannot_confirm_power(self):
        w = self.w
        w._standby_confirmation_after = 0.0
        before = self.f.device.calls['volume']
        w.poll_status(groups={'volume'})
        self.f.wait(lambda: not w.poll_running)
        self.assertEqual(w._standby_confirmation_after, 0.0)
        self.assertEqual(self.f.device.calls['volume'], before)
        self.assert_controls_blocked()

    def test_rejected_standby_restores_controls_without_reviving_volume_target(self):
        w = self.w
        self.f.device.failures['set_power'] = fixture.REMOTE['DeviceResponseError']('TV rejected power request')
        w._desired_volume = 30
        w._volume_rate_loop_running = True
        w.toggle_power()
        self.f.wait(lambda: w.show_error.called and not w._command_workers)
        w.show_error.assert_called_once_with('TV rejected power request')
        self.assertIsNone(w._standby_confirmation_after)
        self.assertTrue(w.volume_up.isEnabled())
        self.assertTrue(w.power_button.isEnabled())
        self.assertTrue(w._focused_shortcut_allowed('Up'))
        self.assertIsNone(w._desired_volume)
        self.assertFalse(w._volume_rate_loop_running)

    def test_queue_rejection_releases_pending_standby(self):
        with patch.object(self.w, 'run_command', return_value=False):
            self.w.toggle_power()
        self.assertIsNone(self.w._standby_confirmation_after)
        self.assertTrue(self.w.volume_up.isEnabled())
        self.assertTrue(self.w.power_button.isEnabled())

    def test_power_connection_failure_stays_offline(self):
        self.f.device.failures['set_power'] = fixture.REMOTE['SonyConnectionError']('TV unavailable')
        self.w.toggle_power()
        self.f.wait(lambda: self.w.show_error.called and not self.w._command_workers)
        self.assertIsNone(self.w.last_power)
        self.assert_controls_blocked()
        self.assertFalse(self.w.power_button.isEnabled())

    def test_pending_standby_withdraws_focused_tv_override_and_smtc(self):
        w = self.w
        w.api.remote_codes['Play'] = 'Play'
        w.config['SMTCPlaybackKeys'] = True
        w.config['AmplifierTVInput'] = 'tv'
        self.assertTrue(w._focused_tv_volume_bindings())
        with patch.object(w, '_playback_key_mode', return_value='smtc'):
            self.assertTrue(w._smtc_commands())
        w._standby_confirmation_after = math.inf
        self.assertEqual(w._focused_tv_volume_bindings(), [])
        with patch.object(w, '_playback_key_mode', return_value='smtc'):
            self.assertFalse(w._smtc_commands())

    def test_pause_retains_pending_standby_for_fresh_recovery_read(self):
        self.w._standby_confirmation_after = math.inf
        self.w._pause_background_work()
        self.assertIsNotNone(self.w._standby_confirmation_after)
        self.assertTrue(math.isfinite(self.w._standby_confirmation_after))


if __name__ == '__main__':
    unittest.main()
