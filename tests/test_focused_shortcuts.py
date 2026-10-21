"""Application-filter shortcuts with real Qt widget, popup and repeat delivery."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut, QTextCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog
import test_graceful_failures as fixture


class FocusedShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.DiscoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start_and_settle()
        self.window = self.fixture.window
        self.app = self.fixture.app
        self.window.activateWindow()
        self.window.power_button.setFocus()
        self.app.processEvents()
        commands = ('Left', 'Right', 'Up', 'Down', 'Confirm', 'Return', 'Home', 'TvInput',
                    'Settings', 'PlayPause', 'Play', 'Pause', 'Stop', 'Next', 'Prev',
                    'ChannelUp', 'ChannelDown', *(f'Num{i}' for i in range(10)))
        self.window.api.remote_codes.update({command: command for command in commands})
        self.window.send_ircc = Mock()
        self.window.adjust_volume = Mock()
        self.window.toggle_mute = Mock()

    def event(self, key, *, kind=QEvent.KeyPress, repeat=False, modifiers=Qt.NoModifier):
        event = QKeyEvent(kind, key, modifiers, '', repeat, 1)
        return self.window.eventFilter(self.window.power_button, event)

    def release(self, key):
        return self.event(key, kind=QEvent.KeyRelease)

    def test_windows_and_other_platforms_share_the_qt_path(self):
        for platform in ('win32', 'linux', 'darwin'):
            with self.subTest(platform=platform), patch.dict(
                    fixture.GLOBALS, sys=SimpleNamespace(platform=platform)):
                QTest.keyClick(self.window.power_button, Qt.Key_Up)
        self.assertEqual(self.window.send_ircc.call_count, 3)
        self.window.send_ircc.assert_called_with('Up')
        self.assertEqual(set(fixture.REMOTE['_NATIVE_VOLUME_KEYS'].values()),
                         {'volume_up', 'volume_down', 'mute'})

    def test_buttons_do_not_consume_remote_arrows_or_enter(self):
        clicked = Mock()
        self.window.power_button.clicked.connect(clicked)
        for key, command in ((Qt.Key_Left, 'Left'), (Qt.Key_Right, 'Right'),
                             (Qt.Key_Up, 'Up'), (Qt.Key_Down, 'Down'), (Qt.Key_Return, 'Confirm')):
            QTest.keyClick(self.window.power_button, key)
            self.window.send_ircc.assert_called_with(command)
        self.assertEqual(self.window.send_ircc.call_count, 5)
        clicked.assert_not_called()

    def test_shortcut_override_prevents_a_competing_qt_shortcut(self):
        shortcut = QShortcut(QKeySequence('Up'), self.window)
        activated = Mock()
        shortcut.activated.connect(activated)
        QTest.keyClick(self.window.power_button, Qt.Key_Up)
        self.window.send_ircc.assert_called_once_with('Up')
        activated.assert_not_called()
        shortcut.deleteLater()

    def test_closed_combo_does_not_navigate_instead_of_tv(self):
        combo = self.window.input_combo
        combo.addItem('Another', 'another')
        combo.setCurrentIndex(0)
        combo.setFocus()
        QTest.keyClick(combo, Qt.Key_Down)
        self.assertEqual(combo.currentIndex(), 0)
        self.window.send_ircc.assert_called_once_with('Down')

    def test_open_popup_owns_arrows_and_enter_through_release(self):
        combo = self.window.input_combo
        combo.addItem('Another', 'another')
        combo.setCurrentIndex(0)
        self.window.set_input = Mock()
        combo.showPopup()
        QTest.keyClick(self.app.focusWidget(), Qt.Key_Down)
        QTest.keyPress(self.app.focusWidget(), Qt.Key_Return)
        self.app.processEvents()
        self.assertFalse(combo.view().window().isVisible())
        # Re-delivery after dismissal still belongs to this same popup press.
        QTest.keyPress(self.window.power_button, Qt.Key_Return)
        self.window.send_ircc.assert_not_called()
        QTest.keyRelease(self.window.power_button, Qt.Key_Return)
        # The offscreen backend does not restore activation after closing a popup.
        self.window.activateWindow()
        self.window.power_button.setFocus()
        self.app.processEvents()
        QTest.keyClick(self.window.power_button, Qt.Key_Return)
        self.window.send_ircc.assert_called_once_with('Confirm')

    def test_text_editing_is_preserved_but_volume_still_routes(self):
        editor = self.window.text_input
        editor.setFocus()
        editor.setPlainText('abc')
        editor.moveCursor(QTextCursor.End)
        QTest.keyClick(editor, Qt.Key_Backspace)
        QTest.keyClick(editor, Qt.Key_Left)
        QTest.keyClick(editor, Qt.Key_Delete)
        self.assertEqual(editor.toPlainText(), 'a')
        self.window.send_ircc.assert_not_called()
        QTest.keyClick(editor, Qt.Key_VolumeUp)
        self.window.adjust_volume.assert_called_once_with(1)

    def test_mapping_keypad_and_transport(self):
        for key, modifiers, command in (
                (Qt.Key_Insert, Qt.NoModifier, 'TvInput'), (Qt.Key_Delete, Qt.NoModifier, 'Settings'),
                (Qt.Key_Home, Qt.NoModifier, 'Home'), (Qt.Key_Enter, Qt.KeypadModifier, 'Confirm'),
                (Qt.Key_1, Qt.KeypadModifier, 'Num1'), (Qt.Key_Plus, Qt.KeypadModifier, 'ChannelUp'),
                (Qt.Key_Minus, Qt.KeypadModifier, 'ChannelDown'),
                (Qt.Key_MediaPlay, Qt.NoModifier, 'Play'), (Qt.Key_MediaPause, Qt.NoModifier, 'Pause'),
                (Qt.Key_MediaStop, Qt.NoModifier, 'Stop'), (Qt.Key_MediaNext, Qt.NoModifier, 'Next'),
                (Qt.Key_MediaPrevious, Qt.NoModifier, 'Prev')):
            QTest.keyClick(self.window.power_button, key, modifiers)
            self.window.send_ircc.assert_called_with(command)

    def test_repeats_and_synthetic_repeat_releases(self):
        for key, command in ((Qt.Key_Up, 'Up'), (Qt.Key_MediaTogglePlayPause, 'PlayPause')):
            self.window.send_ircc.reset_mock()
            self.assertTrue(self.event(key))
            for _ in range(3):
                self.assertTrue(self.event(key, kind=QEvent.KeyRelease, repeat=True))
                self.assertTrue(self.event(key, repeat=True))
            self.assertTrue(self.release(key))
            self.assertEqual(self.window.send_ircc.call_count, 1 if command == 'PlayPause' else 4)
        for key, action, count in ((Qt.Key_VolumeUp, self.window.adjust_volume, 4),
                                   (Qt.Key_VolumeMute, self.window.toggle_mute, 1)):
            action.reset_mock()
            self.assertTrue(self.event(key))
            for _ in range(3):
                self.assertTrue(self.event(key, repeat=True))
            self.assertTrue(self.release(key))
            self.assertEqual(action.call_count, count)

    def test_volume_owner_changes_do_not_acquire_or_duplicate_a_held_press(self):
        with patch.object(self.window, '_amplifier_owns_hotkeys', return_value=True):
            self.assertFalse(self.event(Qt.Key_VolumeUp))
        self.assertFalse(self.event(Qt.Key_VolumeUp, repeat=True))
        self.window.adjust_volume.assert_not_called()
        self.release(Qt.Key_VolumeUp)
        self.assertTrue(self.event(Qt.Key_VolumeUp))
        with patch.object(self.window, '_amplifier_owns_hotkeys', return_value=True):
            self.assertTrue(self.event(Qt.Key_VolumeUp, repeat=True))
        self.window.adjust_volume.assert_called_once_with(1)
        self.assertTrue(self.release(Qt.Key_VolumeUp))

    def test_smtc_playback_is_released_without_disabling_navigation_or_volume(self):
        self.window.config['SMTCPlaybackKeys'] = True
        for key in (Qt.Key_MediaTogglePlayPause, Qt.Key_MediaPlay, Qt.Key_MediaPause,
                    Qt.Key_MediaStop, Qt.Key_MediaNext, Qt.Key_MediaPrevious):
            self.assertFalse(self.event(key))
            self.release(key)
        self.window.send_ircc.assert_not_called()
        QTest.keyClick(self.window.power_button, Qt.Key_Up)
        QTest.keyClick(self.window.power_button, Qt.Key_VolumeDown)
        self.window.send_ircc.assert_called_once_with('Up')
        self.window.adjust_volume.assert_called_once_with(-1)

    def test_calibration_modal_and_inactive_windows_do_not_dispatch(self):
        for state in ('local', 'amplifier'):
            self.window.calibration_active = state == 'local'
            self.window.amplifier_state = {'calibration_active': state == 'amplifier'}
            self.assertFalse(self.event(Qt.Key_Up))
            self.release(Qt.Key_Up)
            self.assertFalse(self.event(Qt.Key_VolumeUp))
            self.release(Qt.Key_VolumeUp)
        self.window.calibration_active = False
        self.window.amplifier_state = None
        dialog = QDialog(self.window)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        self.assertFalse(self.event(Qt.Key_Up))
        self.release(Qt.Key_Up)
        dialog.close()
        dialog.deleteLater()
        self.window.hide()
        self.app.processEvents()
        self.assertFalse(self.event(Qt.Key_Up))
        self.window.send_ircc.assert_not_called()
        self.window.adjust_volume.assert_not_called()

    def test_fast_complete_media_presses_are_all_consumed(self):
        for _ in range(100):
            self.assertTrue(self.event(Qt.Key_VolumeUp))
            self.assertTrue(self.release(Qt.Key_VolumeUp))
        self.assertEqual(self.window.adjust_volume.call_count, 100)

    def test_unavailable_modified_and_mid_hold_keys_are_not_claimed(self):
        self.window.api.remote_codes.pop('Up')
        self.assertFalse(self.event(Qt.Key_Up))
        self.window.api.remote_codes['Up'] = 'Up'
        self.assertFalse(self.event(Qt.Key_Up, repeat=True))
        self.release(Qt.Key_Up)
        self.assertFalse(self.event(Qt.Key_Left, modifiers=Qt.ControlModifier))
        self.release(Qt.Key_Left)
        self.assertFalse(self.event(Qt.Key_Right, repeat=True))
        self.release(Qt.Key_Right)
        self.window.send_ircc.assert_not_called()
        self.assertTrue(self.event(Qt.Key_Up))
        self.release(Qt.Key_Up)

    def test_focus_loss_discards_press_state_without_acquiring_repeats(self):
        self.assertTrue(self.event(Qt.Key_Up))
        self.window.eventFilter(self.window, QEvent(QEvent.WindowDeactivate))
        self.assertFalse(self.event(Qt.Key_Up, repeat=True))
        self.release(Qt.Key_Up)
        self.assertEqual(self.window.send_ircc.call_count, 1)


if __name__ == '__main__':
    unittest.main()
