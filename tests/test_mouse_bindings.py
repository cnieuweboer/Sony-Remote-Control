"""Mouse tokens stay local while Qt guards protect normal widget interaction."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QMainWindow, QPushButton, QTextEdit

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Sony' if (ROOT / 'SonyRemote.pyw').exists() else 'Yamaha'
R = runpy.run_path(str(ROOT / f'{NAME}Remote.pyw'), run_name='mouse_bindings_tests')


class ConfigurationTests(unittest.TestCase):
    def config(self, shortcuts):
        return R['validate_config']({'Shortcuts': shortcuts}, allow_incomplete=True)

    def test_mixed_bindings_compile_separately_and_show_in_tooltips(self):
        config = self.config({'VolumeUp': ['Volume Up', 'WheelUp', 'MouseForward']})
        self.assertEqual(len(R['_compile_shortcuts'](config)), len(R['_compile_shortcuts'](R['_default_config_copy']())))
        self.assertEqual(R['_compile_mouse_shortcuts'](config),
                         {'WheelUp': 'volume_up', 'MouseForward': 'volume_up'})
        self.assertIn('Mouse wheel up', R['_shortcut_tip'](config, 'volume_up'))
        self.assertIn('Mouse forward button', R['_shortcut_tip'](config, 'volume_up'))

    def test_all_tokens_are_case_insensitive_and_do_not_enter_keyboard_maps(self):
        for name in R['MOUSE_BINDING_LABELS']:
            with self.subTest(name=name):
                config = self.config({'Mute': [name.lower()]})
                self.assertEqual(config['Shortcuts']['Mute'], [name])
                self.assertEqual(R['_compile_mouse_shortcuts'](config), {name: 'mute'})
                self.assertNotIn('mute', R['_compile_shortcuts'](config).values())

    def test_duplicate_mouse_bindings_are_rejected(self):
        for shortcuts in ({'VolumeUp': ['WheelUp'], 'VolumeDown': ['wheelup']},
                          {'Mute': ['MouseMiddle', 'mousemiddle']}):
            with self.assertRaisesRegex(R['ConfigError'], 'assigned more than once'):
                self.config(shortcuts)

    def test_left_right_and_modified_mouse_bindings_are_not_supported(self):
        for name in ('MouseLeft', 'MouseRight', 'Ctrl+WheelUp', 'Shift+MouseBack'):
            with self.subTest(name=name), self.assertRaises(R['ConfigError']):
                self.config({'Mute': [name]})

    @unittest.skipUnless(NAME == 'Yamaha', 'Amplifier global shortcuts')
    def test_global_mouse_bindings_are_explicitly_rejected(self):
        with self.assertRaisesRegex(R['ConfigError'], 'not GlobalShortcuts'):
            R['validate_config']({'GlobalShortcuts': {'VolumeUp': ['WheelUp']}}, allow_incomplete=True)

    @unittest.skipUnless(NAME == 'Sony', 'TV-advertised actions')
    def test_arbitrary_tv_action_can_use_mouse_binding(self):
        config = self.config({'Guide': ['MouseBack']})
        self.assertEqual(R['_compile_mouse_shortcuts'](config), {'MouseBack': 'ircc:Guide'})


class MouseEventTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.window = QMainWindow()
        self.window._shutting_down = False
        self.window.resize(250, 200)
        self.button = QPushButton('Target', self.window)
        self.button.resize(100, 30)
        self.window.show()
        self.window.activateWindow()
        self.app.processEvents()
        self.dispatch = Mock(return_value=True)
        self.handler = R['MouseShortcuts'](self.window,
            {name: name for name in R['MOUSE_BINDING_LABELS']}, self.dispatch)
        self.addCleanup(self.window.close)

    def mouse(self, button, kind=QEvent.MouseButtonPress, watched=None, modifiers=Qt.NoModifier):
        watched = self.button if watched is None else watched
        point = QPointF(5, 5)
        event = QMouseEvent(kind, point, point, button,
                           Qt.NoButton if kind == QEvent.MouseButtonRelease else button, modifiers)
        return self.handler.handle(watched, event)

    def wheel(self, x=0, y=120, watched=None, modifiers=Qt.NoModifier, pixels=None):
        point = QPointF(5, 5)
        event = QWheelEvent(point, point, QPoint() if pixels is None else pixels,
                            QPoint(x, y), Qt.NoButton, modifiers, Qt.NoScrollPhase, False)
        return self.handler.handle(self.button if watched is None else watched, event)

    def test_middle_back_forward_dispatch_once_per_click_and_consume_release(self):
        for button, name in ((Qt.MiddleButton, 'MouseMiddle'), (Qt.BackButton, 'MouseBack'),
                             (Qt.ForwardButton, 'MouseForward')):
            self.dispatch.reset_mock()
            self.assertTrue(self.mouse(button))
            self.assertTrue(self.mouse(button))  # Duplicate held press is not another action.
            self.assertTrue(self.mouse(button, QEvent.MouseButtonRelease))
            self.dispatch.assert_called_once_with(name)

    def test_left_and_right_clicks_remain_normal(self):
        self.assertFalse(self.mouse(Qt.LeftButton))
        self.assertFalse(self.mouse(Qt.RightButton))
        self.dispatch.assert_not_called()

    def test_both_wheel_axes_and_multiple_notches(self):
        for x, y, name in ((0, 120, 'WheelUp'), (0, -120, 'WheelDown'),
                            (120, 0, 'WheelRight'), (-120, 0, 'WheelLeft')):
            self.dispatch.reset_mock()
            self.assertTrue(self.wheel(x, y))
            self.dispatch.assert_called_once_with(name)
        self.dispatch.reset_mock()
        self.assertTrue(self.wheel(y=360))
        self.assertEqual(self.dispatch.call_count, 3)

    def test_high_resolution_deltas_accumulate_without_parent_propagation(self):
        for _ in range(3):
            self.assertTrue(self.wheel(y=30))
        self.dispatch.assert_not_called()
        self.assertTrue(self.wheel(y=30))
        self.dispatch.assert_called_once_with('WheelUp')

    def test_direction_reversal_discards_old_partial_delta(self):
        self.wheel(y=90)
        for _ in range(4):
            self.wheel(y=-30)
        self.dispatch.assert_called_once_with('WheelDown')

    def test_dropdown_and_text_fields_keep_normal_mouse_behavior(self):
        combo = QComboBox(self.window)
        line = QLineEdit(self.window)
        editor = QTextEdit(self.window)
        for widget in (combo, line, editor.viewport()):
            with self.subTest(widget=type(widget).__name__):
                self.assertFalse(self.mouse(Qt.MiddleButton, watched=widget))
                self.assertFalse(self.wheel(watched=widget))
        self.dispatch.assert_not_called()

    def test_popup_modal_and_inactive_context_do_not_dispatch(self):
        for name in ('activePopupWidget', 'activeModalWidget'):
            with patch.object(R['QApplication'], name, return_value=self.button):
                self.assertFalse(self.mouse(Qt.BackButton))
                self.assertFalse(self.wheel())
        with patch.object(self.window, 'isActiveWindow', return_value=False):
            self.assertFalse(self.wheel())
        self.window._shutting_down = True
        self.assertFalse(self.mouse(Qt.BackButton))
        self.dispatch.assert_not_called()

    def test_events_in_another_window_do_not_dispatch(self):
        other = QMainWindow()
        self.addCleanup(other.close)
        self.assertFalse(self.wheel(watched=other))
        self.dispatch.assert_not_called()

    def test_modifiers_pixel_only_and_unbound_events_are_not_claimed(self):
        self.assertFalse(self.wheel(modifiers=Qt.ControlModifier))
        self.assertFalse(self.mouse(Qt.BackButton, modifiers=Qt.ShiftModifier))
        self.assertFalse(self.wheel(y=0, pixels=QPoint(0, 20)))
        self.handler.bindings.clear()
        self.assertFalse(self.wheel())
        self.dispatch.assert_not_called()

    def test_rejected_actions_do_not_consume_full_events(self):
        self.dispatch.return_value = False
        self.assertFalse(self.mouse(Qt.BackButton))
        self.assertFalse(self.wheel())

    def test_partly_handled_wheel_event_is_not_propagated_and_repeated(self):
        self.dispatch.side_effect = [True, False]
        self.assertTrue(self.wheel(y=240))
        self.assertEqual(self.dispatch.call_count, 2)


class RemoteIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_remote_filter_dispatches_mouse_through_its_existing_action_path(self):
        if NAME == 'Sony':
            import test_focused_shortcuts as fixture
            fixture.FocusedShortcutTests.setUpClass()
            case = fixture.FocusedShortcutTests()
            case.setUp()
            self.addCleanup(case.doCleanups)
            window = case.window
            window._mouse_shortcuts.bindings = {'WheelUp': 'volume_up', 'MouseBack': 'Prev'}
            action = window.adjust_volume
        else:
            import test_volume_controls as fixture
            fixture.VolumeControlTests.setUpClass()
            case = fixture.VolumeControlTests()
            window = case.window()
            self.addCleanup(case.doCleanups)
            window.activateWindow()
            self.app.processEvents()
            window._enqueue_hotkey_locked = Mock(return_value=True)
            window.connected = True
            window.last_status = dict(power='on', volume=80, mute=False)
            window._last_status_at = R['time'].monotonic()
            window._mouse_shortcuts.bindings = {'WheelUp': 'volume_up', 'MouseMiddle': 'mute'}
            window.power_button.setEnabled(True)
            action = window._enqueue_hotkey_locked
        point = QPointF(5, 5)
        event = QWheelEvent(point, point, QPoint(), QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
        self.assertTrue(window.eventFilter(window.power_button, event))
        action.assert_called_once_with(1 if NAME == 'Sony' else 'volume_up')
        if NAME == 'Sony':
            event = QMouseEvent(QEvent.MouseButtonPress, point, point, Qt.BackButton, Qt.BackButton, Qt.NoModifier)
            self.assertTrue(window.eventFilter(window.power_button, event))
            window.send_ircc.assert_called_once_with('Prev')
        else:
            window.calibration_active = True
            event = QMouseEvent(QEvent.MouseButtonPress, point, point, Qt.MiddleButton, Qt.MiddleButton, Qt.NoModifier)
            self.assertFalse(window.eventFilter(window.power_button, event))


if __name__ == '__main__':
    unittest.main()
