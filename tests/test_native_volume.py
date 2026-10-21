"""Focused Windows volume fallback: native consumption and queued Qt delivery."""
import ctypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
import test_graceful_failures as fixture

REMOTE = fixture.REMOTE


class NativeVolumeCallbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        self.accept = Mock(return_value=True)
        self.hook = REMOTE['_WindowsVolumeHook'](self.accept)
        self.hook._user32 = SimpleNamespace(
            GetForegroundWindow=Mock(return_value=123),
            CallNextHookEx=Mock(return_value=17))
        self.hook._notify = Mock()

    def key(self, key, message=0x100):
        data = REMOTE['_LowLevelKey'](key, 0, 0, 0, 0)
        return self.hook._handle_key(0, message, ctypes.addressof(data))

    def test_only_volume_keys_are_intercepted(self):
        self.assertEqual(set(REMOTE['_NATIVE_VOLUME_KEYS']), {0xAD, 0xAE, 0xAF})
        for key in (0x25, 0x26, 0x0D, 0xB0, 0xB1, 0xB2, 0xB3):
            self.assertEqual(self.key(key), 17)
        self.accept.assert_not_called()
        for key, command in ((0xAF, 'volume_up'), (0xAE, 'volume_down'), (0xAD, 'mute')):
            self.assertEqual(self.key(key), 1)
            self.assertEqual(self.key(key, 0x101), 1)
            self.accept.assert_called_with(command, 123)

    def test_fast_volume_presses_suppress_both_edges(self):
        for _ in range(100):
            self.assertEqual(self.key(0xAF), 1)
            self.assertEqual(self.key(0xAF, 0x101), 1)
        self.assertEqual(self.accept.call_count, 100)
        self.hook._user32.CallNextHookEx.assert_not_called()

    def test_mute_toggles_once_and_owned_repeats_remain_suppressed(self):
        for _ in range(5):
            self.assertEqual(self.key(0xAD), 1)
        self.assertEqual(self.accept.call_count, 1)
        self.assertEqual(self.key(0xAD, 0x101), 1)
        self.assertEqual(self.key(0xAF), 1)
        self.accept.return_value = False
        self.assertEqual(self.key(0xAF), 1)
        self.assertEqual(self.key(0xAF, 0x101), 1)

    def test_unowned_press_stays_forwarded_until_release(self):
        self.accept.return_value = False
        self.assertEqual(self.key(0xAF), 17)
        self.accept.return_value = True
        self.assertEqual(self.key(0xAF), 17)
        self.assertEqual(self.key(0xAF, 0x101), 17)
        self.assertEqual(self.accept.call_count, 1)
        self.assertEqual(self.key(0xAF), 1)


class FocusedNativeVolumeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.DiscoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start_and_settle()
        self.window = self.fixture.window
        self.window.activateWindow()
        self.window.power_button.setFocus()
        self.fixture.app.processEvents()
        self.window.adjust_volume = Mock()
        self.window.toggle_mute = Mock()
        self.window._publish_native_volume_state()
        self.handle = int(self.window.winId())

    def test_native_delivery_once_while_windows_qt_volume_events_are_ignored(self):
        for command in ('volume_up', 'volume_down', 'mute'):
            self.assertTrue(self.window._accept_native_volume(command, self.handle))
        self.fixture.app.processEvents()
        self.assertEqual([call.args for call in self.window.adjust_volume.call_args_list], [(1,), (-1,)])
        self.window.toggle_mute.assert_called_once()
        self.window.adjust_volume.reset_mock()
        self.window.toggle_mute.reset_mock()
        with patch.dict(fixture.GLOBALS, sys=SimpleNamespace(platform='win32')):
            for key in (Qt.Key_VolumeUp, Qt.Key_VolumeDown, Qt.Key_VolumeMute):
                for kind in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease):
                    event = QKeyEvent(kind, key, Qt.NoModifier)
                    self.assertFalse(self.window.eventFilter(self.window, event))
        self.window.adjust_volume.assert_not_called()
        self.window.toggle_mute.assert_not_called()
        self.assertNotIn('_WindowsMessageHeader', REMOTE)
        self.assertFalse(hasattr(self.window, '_handle_windows_media_command'))

    def test_queue_overflow_drops_steps_without_forwarding_to_windows(self):
        for _ in range(100):
            self.assertTrue(self.window._accept_native_volume('volume_up', self.handle))
        self.assertEqual(len(self.window._native_volume_queue), 32)
        self.fixture.app.processEvents()
        self.assertEqual(self.window.adjust_volume.call_count, 32)
        self.assertEqual(len(self.window._native_volume_queue), 0)

    def test_global_owner_wrong_foreground_and_expired_ownership(self):
        self.assertFalse(self.window._accept_native_volume('volume_up', self.handle + 1))
        self.assertFalse(self.window._accept_native_volume('Up', self.handle))
        self.window.amplifier_client.state_deadline = fixture.time.monotonic() + 10
        with patch.object(self.window, '_amplifier_owns_hotkeys', return_value=True):
            self.window._publish_native_volume_state()
        self.window._native_owned_codes = None  # Legacy producer owns physical volume keys.
        self.assertIsNone(self.window._resolve_native_volume(0xAF, 0))
        with self.window._native_volume_lock:
            self.window._native_volume_state = (self.handle, False, fixture.time.monotonic() - 1)
        self.assertTrue(self.window._accept_native_volume('volume_up', self.handle))
        self.fixture.app.processEvents()
        self.window.adjust_volume.assert_called_once_with(1)

    def test_popup_calibration_hidden_and_pause_block_and_clear_native_queue(self):
        self.assertTrue(self.window._accept_native_volume('volume_up', self.handle))
        self.window.calibration_active = True
        self.window._publish_native_volume_state()
        self.assertEqual(len(self.window._native_volume_queue), 0)
        self.assertFalse(self.window._accept_native_volume('volume_up', self.handle))
        self.window.calibration_active = False
        self.window.input_combo.showPopup()
        self.window._publish_native_volume_state()
        self.assertFalse(self.window._accept_native_volume('volume_up', self.handle))
        self.window.input_combo.hidePopup()
        self.window.hide()
        self.window._publish_native_volume_state()
        self.assertFalse(self.window._accept_native_volume('volume_up', self.handle))
        self.fixture.app.processEvents()
        self.window.adjust_volume.assert_not_called()

    def test_restart_pause_blocks_hook_and_recovery_restores_it(self):
        active = self.window._pause_background_work()
        self.assertFalse(self.window._accept_native_volume('volume_up', self.handle))
        self.window._resume_background_work(active)
        self.window.activateWindow()
        self.fixture.app.processEvents()
        self.window._publish_native_volume_state()
        self.assertTrue(self.window._accept_native_volume('volume_up', self.handle))


if __name__ == '__main__':
    unittest.main()
