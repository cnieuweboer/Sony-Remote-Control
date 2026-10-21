"""Fake WinRT integration; actual Windows routing needs desktop testing."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from enum import Enum
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'SonyRemote.pyw'), run_name='smtc_tests')
GLOBALS = REMOTE['SMTCPlayback'].start.__globals__


class Status(Enum):
    PLAYING = 1
    PAUSED = 2
    STOPPED = 3


class Controls:
    def __init__(self):
        self.is_enabled = True
        self.callback = None
        self.removed = []
        self.writes = []
        self.fail_disable = False
        self.fail_remove = False
        self.fail_register = False

    def __setattr__(self, name, value):
        if name == 'is_enabled' and not value and getattr(self, 'fail_disable', False):
            raise OSError('disable failed')
        if name.startswith('is_') or name == 'playback_status':
            if hasattr(self, 'writes'):
                self.writes.append((name, threading.get_ident()))
        object.__setattr__(self, name, value)

    def add_button_pressed(self, callback):
        if self.fail_register:
            raise OSError('registration failed')
        self.callback = callback
        return 42

    def remove_button_pressed(self, token):
        self.removed.append(token)
        if self.fail_remove:
            raise OSError('unsubscribe failed')

    def press(self, name):
        self.callback(self, SimpleNamespace(button=SimpleNamespace(name=name)))


class AdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.controls = Controls()
        self.get_window = Mock(side_effect=lambda hwnd: self.controls)
        self.loader = Mock(return_value=(Status, self.get_window))
        context = patch.dict(GLOBALS, _load_smtc_api=self.loader)
        context.start()
        self.addCleanup(context.stop)
        self.adapter = REMOTE['SMTCPlayback']()
        self.buttons = QSignalSpy(self.adapter.button_pressed)
        self.errors = QSignalSpy(self.adapter.failed)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.adapter.stop()
        self.adapter.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def start(self, commands=('Play', 'Pause', 'Stop', 'Next', 'Prev')):
        self.adapter.start(1234)
        self.adapter.update(commands)

    def test_exposes_only_supported_playback_commands_including_stop(self):
        self.start(('Play', 'Stop', 'Next'))
        self.get_window.assert_called_once_with(1234)
        for name in ('play', 'stop', 'next'):
            self.assertTrue(getattr(self.controls, f'is_{name}_enabled'))
        for name in ('pause', 'previous', 'record', 'rewind', 'fast_forward'):
            self.assertFalse(getattr(self.controls, f'is_{name}_enabled'))
        self.adapter.update(())
        self.assertFalse(self.controls.is_enabled)

    def test_each_callback_is_queued_to_qt_without_winrt_writes(self):
        self.start()
        self.controls.writes.clear()
        def receive():
            for name in ('PLAY', 'PAUSE', 'STOP', 'NEXT', 'PREVIOUS', 'REWIND'):
                self.controls.press(name)
        worker = threading.Thread(target=receive)
        worker.start()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.buttons.count(), 0)
        self.assertEqual(self.controls.writes, [])
        self.app.processEvents()
        self.assertEqual([self.buttons.at(i)[0] for i in range(self.buttons.count())],
                         ['Play', 'Pause', 'Stop', 'Next', 'Prev'])

    def test_queued_events_are_ignored_after_disable(self):
        self.start()
        self.controls.press('NEXT')
        self.adapter.update(())
        self.app.processEvents()
        self.assertEqual(self.buttons.count(), 0)

    def test_retired_callback_cannot_act_after_restart(self):
        self.start()
        old_controls = self.controls
        old_controls.press('NEXT')
        self.adapter.stop()
        self.controls = Controls()
        self.start()
        old_controls.press('PLAY')
        self.controls.press('STOP')
        self.app.processEvents()
        self.assertEqual(self.buttons.count(), 1)
        self.assertEqual(self.buttons.at(0), ['Stop'])
        self.assertFalse(old_controls.is_enabled)
        self.assertEqual(old_controls.removed, [42])

    def test_note_command_updates_optimistic_status_in_gui_thread(self):
        self.start()
        for command, expected in (('Pause', Status.PAUSED), ('Play', Status.PLAYING),
                                  ('PlayPause', Status.PAUSED), ('PlayPause', Status.PLAYING),
                                  ('Stop', Status.STOPPED)):
            self.adapter.note_command(command)
            self.assertEqual(self.controls.playback_status, expected)
        self.assertTrue(all(thread == threading.get_ident() for _, thread in self.controls.writes))

    def test_stop_attempts_unsubscribe_even_if_disable_fails(self):
        self.start()
        self.controls.fail_disable = True
        with patch.dict(GLOBALS, record_exception=Mock()):
            self.adapter.stop()
        self.controls.fail_disable = False
        self.assertEqual(self.controls.removed, [42])
        self.assertIsNone(self.adapter.controls)

    def test_failed_registration_disables_partially_created_session(self):
        self.controls.fail_register = True
        with self.assertRaises(OSError):
            self.adapter.start(1234)
        self.assertFalse(self.controls.is_enabled)
        self.assertIsNone(self.adapter.controls)

    def test_missing_import_leaves_no_session(self):
        self.loader.side_effect = ModuleNotFoundError('not installed')
        with self.assertRaises(ModuleNotFoundError):
            self.adapter.start(1234)
        self.assertIsNone(self.adapter.controls)

    def test_sdk_update_failure_disables_session_and_reports_error(self):
        self.start()
        self.controls.fail_disable = True
        with patch.dict(GLOBALS, record_exception=Mock()):
            self.adapter.update(())
        self.controls.fail_disable = False
        self.assertEqual(self.errors.count(), 1)
        self.assertIsNone(self.adapter.controls)
        self.assertEqual(self.controls.removed, [42])


# Reuse the device-free window fixture without inheriting its unrelated tests.
import test_graceful_failures as window_fixture


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        window_fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        self.fixture = window_fixture.DiscoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.window = self.fixture.window
        self.fixture.start_and_settle()
        self.fixture.wait(lambda: not self.window._workers)
        self.controls = []
        def create(hwnd):
            controls = Controls()
            self.controls.append(controls)
            return controls
        self.loader = Mock(return_value=(Status, create))
        self.context = patch.dict(window_fixture.GLOBALS,
            _load_smtc_api=self.loader, sys=SimpleNamespace(platform='win32'))
        self.context.start()
        # Stop before removing the patched fake SDK; the fixture then closes.
        self.addCleanup(self.context.stop)
        self.addCleanup(lambda: self.window._smtc.stop() if self.window._smtc else None)
        self.window._show_smtc_warning = Mock()
        self.window.api.ircc = Mock()
        self.window.api.remote_codes.update({name: name for name in
                                             ('Play', 'Pause', 'Stop', 'Next', 'Prev')})

    def enable(self):
        self.window.config['SMTCPlaybackKeys'] = True
        self.window._setup_smtc()
        return self.controls[-1]

    def test_default_off_does_not_load_sdk_and_keeps_focused_keys(self):
        self.window._setup_smtc()
        self.loader.assert_not_called()
        self.assertEqual(self.window._playback_key_mode(), 'focused')
        self.window._refresh_playback_routing()
        self.assertTrue(all(self.window._focused_shortcut_allowed(command)
                            for command in ('PlayPause', 'Stop', 'Next', 'Prev')))

    def test_smtc_excludes_focused_playback_keys_but_keeps_volume_and_navigation(self):
        controls = self.enable()
        self.assertTrue(controls.is_enabled)
        allowed = {command for command in ('PlayPause', 'Stop', 'Next', 'Prev',
                                          'volume_up', 'volume_down', 'mute', 'Up', 'Confirm')
                   if self.window._focused_shortcut_allowed(command)}
        self.assertFalse(REMOTE['_PLAYBACK_KEY_COMMANDS'] & allowed)
        self.assertTrue({'volume_up', 'volume_down', 'mute', 'Up', 'Confirm'} <= allowed)
        for command in ('PlayPause', 'Stop', 'Next', 'Prev'):
            self.assertFalse(self.window._focused_shortcut_allowed(command))
            self.assertFalse(self.window._dispatch_shortcut(command))
        self.window.api.ircc.assert_not_called()

    def test_smtc_stop_and_play_are_serialized_and_update_routing_hint(self):
        controls = self.enable()
        controls.press('PLAY')
        controls.press('PAUSE')
        controls.press('STOP')
        self.fixture.wait(lambda: self.window.api.ircc.call_count == 3 and not self.window._command_workers)
        self.assertEqual([call.args[0] for call in self.window.api.ircc.call_args_list],
                         ['Play', 'Pause', 'Stop'])
        self.assertEqual(controls.playback_status, Status.STOPPED)
        self.window.show_error.assert_not_called()

    def test_hidden_window_keeps_existing_polling_and_global_playback(self):
        controls = self.enable()
        self.window.hide()
        self.assertTrue(self.window.poll_timer.isActive())
        previous = self.window._status_sequence
        self.window.poll_status()
        self.fixture.wait(lambda: self.window._status_sequence > previous)
        controls.press('NEXT')
        self.fixture.wait(lambda: self.window.api.ircc.called)
        self.window.api.ircc.assert_called_once_with('Next')

    def test_unavailable_explicit_commands_are_not_advertised_or_toggled(self):
        del self.window.api.remote_codes['Play']
        del self.window.api.remote_codes['Pause']
        controls = self.enable()
        self.assertFalse(controls.is_play_enabled)
        self.assertFalse(controls.is_pause_enabled)
        controls.press('PLAY')
        controls.press('PAUSE')
        self.fixture.app.processEvents()
        self.window.api.ircc.assert_not_called()
        # The on-screen toggle remains usable independently of key ownership.
        self.window.send_ircc('PlayPause')
        self.fixture.wait(lambda: self.window.api.ircc.called)
        self.window.api.ircc.assert_called_once_with('PlayPause')

    def test_standby_offline_and_calibration_disable_participation(self):
        controls = self.enable()
        for power, calibrated in (('standby', False), (None, False), ('active', True)):
            self.window.last_power = power
            self.window.calibration_active = calibrated
            self.window._refresh_playback_routing()
            self.assertFalse(controls.is_enabled)
            controls.press('NEXT')
            self.fixture.app.processEvents()
            self.window.api.ircc.assert_not_called()
        self.window.calibration_active = False
        self.window.last_power = 'active'
        self.window._refresh_playback_routing()
        self.assertTrue(controls.is_enabled)

    def test_queued_button_is_rechecked_after_state_changes(self):
        controls = self.enable()
        controls.press('STOP')
        self.window.last_power = 'standby'
        self.fixture.app.processEvents()
        self.window.api.ircc.assert_not_called()

    def test_missing_sdk_warns_once_and_never_falls_back_to_focused_keys(self):
        self.loader.side_effect = ModuleNotFoundError('not installed')
        self.window.config['SMTCPlaybackKeys'] = True
        self.window._setup_smtc()
        self.fixture.app.processEvents()
        self.assertEqual(self.window._playback_key_mode(), 'unavailable')
        self.assertFalse(any(self.window._focused_shortcut_allowed(command)
                             for command in REMOTE['_PLAYBACK_KEY_COMMANDS']))
        self.window._setup_smtc()
        self.fixture.app.processEvents()
        self.window._show_smtc_warning.assert_called_once_with()
        self.assertTrue(self.window.power_button.isEnabled())

    def test_non_windows_option_does_not_import_sdk(self):
        self.window.config['SMTCPlaybackKeys'] = True
        with patch.dict(window_fixture.GLOBALS, sys=SimpleNamespace(platform='linux')):
            self.window._setup_smtc()
            self.assertEqual(self.window._playback_key_mode(), 'unavailable')
        self.loader.assert_not_called()

    def test_pause_and_resume_restore_smtc_session(self):
        controls = self.enable()
        timers = self.window._pause_background_work()
        self.assertFalse(controls.is_enabled)
        self.assertEqual(controls.removed, [42])
        self.assertIsNone(self.window._smtc.controls)
        with patch.object(window_fixture.REMOTE['SonyRemote'], '_setup_activation'), \
             patch.object(window_fixture.REMOTE['SonyRemote'], '_setup_calibration'), \
             patch.object(window_fixture.REMOTE['AmplifierClient'], 'start'):
            self.window._resume_background_work(timers)
        self.assertEqual(self.loader.call_count, 2)
        self.assertTrue(self.controls[-1].is_enabled)
        self.assertFalse(self.window._shutting_down)

    def test_empty_remote_code_discovery_releases_smtc_until_recovery(self):
        controls = self.enable()
        self.window._discovery_ready.discard('remote_codes')
        self.window._refresh_playback_routing()
        self.assertFalse(controls.is_enabled)
        self.window._discovery_ready.add('remote_codes')
        self.window._refresh_playback_routing()
        self.assertTrue(controls.is_enabled)

    def test_registration_works_without_ever_showing_window(self):
        config = window_fixture.REMOTE['_default_config_copy']()
        config.update(SMTCPlaybackKeys=True, Mica=False)
        cls = window_fixture.REMOTE['SonyRemote']
        with patch.object(cls, '_setup_activation', lambda self: None), \
             patch.object(cls, '_setup_calibration', lambda self: None), \
             patch.object(window_fixture.REMOTE['AmplifierClient'], 'start', lambda self: None):
            hidden = cls(config)
        def cleanup():
            hidden._shutdown()
            hidden.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.addCleanup(cleanup)
        self.assertFalse(hidden.isVisible())
        self.assertIsNotNone(hidden._smtc.controls)
        self.assertFalse(hidden._smtc.controls.is_enabled)
        self.assertTrue(hidden._smtc_background_polling())

    def test_failed_replacement_launch_restores_smtc(self):
        controls = self.enable()
        cls = window_fixture.REMOTE['SonyRemote']
        with patch.object(cls, '_setup_activation'), patch.object(cls, '_setup_calibration'), \
             patch.object(window_fixture.REMOTE['AmplifierClient'], 'start'), \
             patch.dict(window_fixture.GLOBALS,
                        release_single_instance=Mock(), acquire_single_instance=Mock(return_value=True),
                        launch_replacement=Mock(side_effect=OSError('launch failed'))), \
             patch.object(window_fixture.REMOTE['QMessageBox'], 'warning'):
            self.window._restart_for_config_change()
        self.assertFalse(controls.is_enabled)
        self.assertTrue(self.controls[-1].is_enabled)
        self.assertFalse(self.window._shutting_down)
        self.assertTrue(self.window._restart_failed)
        self.assertEqual(self.loader.call_count, 2)

    def test_amplifier_volume_key_ownership_remains_separate(self):
        self.enable()
        self.window.amplifier_client.state_deadline = window_fixture.time.monotonic() + 5
        with patch.object(self.window, '_amplifier_owns_hotkeys', return_value=True):
            self.window._refresh_playback_routing()
            self.assertFalse(self.window._focused_shortcut_allowed('volume_up'))
        self.window.api.ircc.assert_not_called()
        self.assertEqual(self.window._playback_key_mode(), 'smtc')

    def test_queue_rejection_does_not_change_playback_hint(self):
        controls = self.enable()
        with patch.object(self.window, 'run_command', return_value=False):
            self.assertFalse(self.window.send_ircc('Pause', background=True))
        self.assertEqual(controls.playback_status, Status.PLAYING)
        self.window.api.ircc.assert_not_called()

    def test_smtc_command_errors_use_non_modal_feedback(self):
        controls = self.enable()
        self.window.api.ircc.side_effect = window_fixture.REMOTE['DeviceResponseError']('Rejected')
        controls.press('STOP')
        self.fixture.wait(lambda: self.window.api.ircc.called and not self.window._command_workers)
        self.window.show_error.assert_not_called()
        self.assertIn('Rejected', self.window.power_button.toolTip())



if __name__ == '__main__':
    unittest.main()
