"""Real Qt text-focus shortcuts, empty submissions and ownership after Enter."""
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
import test_focused_shortcuts as fixture


class TextShortcutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FocusedShortcutTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.FocusedShortcutTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w, self.app = self.fixture.window, self.fixture.app
        self.g = type(self.w).__init__.__globals__
        self.editor = self.w.text_input

    def test_shift_enter_focuses_editor_and_is_a_newline_inside_it(self):
        for key,modifiers in ((Qt.Key_Return,Qt.ShiftModifier),
                              (Qt.Key_Enter,Qt.ShiftModifier|Qt.KeypadModifier)):
            self.w.power_button.setFocus()
            self.editor.clear()
            QTest.keyClick(self.w.power_button,key,modifiers)
            self.assertTrue(self.editor.hasFocus())
            self.assertEqual(self.editor.toPlainText(),'')
            QTest.keyClick(self.editor,key,modifiers)
            self.assertEqual(self.editor.toPlainText(),'\n')
        self.w.send_ircc.assert_not_called()

    def test_focus_shortcut_is_configurable_and_can_be_disabled(self):
        self.w.config['Shortcuts']['FocusTextInput'] = ['F8']
        self.w._shortcut_bindings = self.g['_compile_shortcuts'](self.w.config)
        QTest.keyClick(self.w.power_button,Qt.Key_F8)
        self.assertTrue(self.editor.hasFocus())
        self.w.power_button.setFocus()
        QTest.keyClick(self.w.power_button,Qt.Key_Return,Qt.ShiftModifier)
        self.assertFalse(self.editor.hasFocus())
        self.w.config['Shortcuts']['FocusTextInput'] = []
        self.w._shortcut_bindings = self.g['_compile_shortcuts'](self.w.config)
        QTest.keyClick(self.w.power_button,Qt.Key_F8)
        self.assertFalse(self.editor.hasFocus())

    def test_popup_calibration_and_disabled_editor_keep_their_guards(self):
        for blocked in ('popup','calibration','disabled'):
            with self.subTest(blocked=blocked):
                self.w.power_button.setFocus()
                if blocked=='calibration':
                    self.w.calibration_active = True
                elif blocked=='disabled':
                    self.editor.setEnabled(False)
                else:
                    self.w.input_combo.showPopup()
                self.assertFalse(self.w._focused_shortcut_allowed('focus_text_input'))
                self.w.input_combo.hidePopup()
                self.w.calibration_active = False
                self.editor.setEnabled(True)

    def test_accepted_enter_and_ctrl_enter_release_focus_without_clearing_text(self):
        for modifiers in (Qt.NoModifier,Qt.ControlModifier):
            with self.subTest(modifiers=modifiers),patch.object(self.w,'run_command',return_value=True) as run:
                self.editor.setPlainText('keep me')
                self.editor.setFocus()
                QTest.keyClick(self.editor,Qt.Key_Return,modifiers)
                run.assert_called_once()
                self.assertFalse(self.editor.hasFocus())
                self.assertTrue(self.w.power_button.hasFocus())
                self.assertEqual(self.editor.toPlainText(),'keep me')
                QTest.keyClick(self.w.power_button,Qt.Key_Up)
                self.w.send_ircc.assert_called_with('Up')

    def test_rejected_or_calibration_blocked_submission_keeps_focus(self):
        for blocked in ('queue','calibration'):
            with self.subTest(blocked=blocked),patch.object(self.w,'run_command',return_value=False) as run:
                self.editor.setPlainText('retain')
                self.editor.setFocus()
                self.w.calibration_active = blocked=='calibration'
                QTest.keyClick(self.editor,Qt.Key_Return)
                self.assertTrue(self.editor.hasFocus())
                self.assertEqual(self.editor.toPlainText(),'retain')
                if blocked=='calibration':
                    run.assert_not_called()
                self.w.calibration_active = False

    def test_empty_string_reaches_set_text_form_for_both_submission_paths(self):
        self.editor.clear()
        for method in (self.w.send_text_input,self.w.send_text_input_and_confirm):
            self.w.config['TextInputSubmit'] = False
            with self.subTest(method=method.__name__),patch.object(self.w,'run_command',return_value=True) as run, \
                 patch.object(self.w.api,'set_text_form',create=True) as send:
                self.editor.setFocus()
                method()
                run.assert_called_once()
                run.call_args.args[0]()
                send.assert_called_once_with('')
                self.assertFalse(self.editor.hasFocus())

    def test_empty_ctrl_enter_keeps_the_full_submit_sequence(self):
        self.editor.clear()
        self.w.config['TextInputSubmit'] = True
        with patch.object(self.w,'run_command',return_value=True) as run, \
             patch.object(self.w.api,'set_text_form',create=True) as send, \
             patch.object(self.w.api,'is_text_input_active',return_value=False,create=True), \
             patch.object(self.w.api,'wait_for_text_input_state',return_value=True,create=True), \
             patch.object(self.w.api,'validate_remote_commands',create=True), \
             patch.object(self.w.api,'ircc',create=True) as ircc:
            self.w.send_text_input_and_confirm()
            run.call_args.args[0]()
            send.assert_called_once_with('')
            self.assertEqual([call.args[0] for call in ircc.call_args_list],
                             ['Confirm',*self.w.config['TextInputSubmitSequence']])

    def test_holding_submitting_enter_does_not_click_power_or_send_confirm(self):
        clicked = Mock()
        self.w.power_button.clicked.connect(clicked)
        self.editor.setFocus()
        with patch.object(self.w,'run_command',return_value=True) as run:
            QTest.keyPress(self.editor,Qt.Key_Return)
            self.assertFalse(self.editor.hasFocus())
            for _ in range(5):
                event = QKeyEvent(QEvent.KeyPress,Qt.Key_Return,Qt.NoModifier,'',True,1)
                self.app.sendEvent(self.w.power_button,event)
            QTest.keyRelease(self.w.power_button,Qt.Key_Return)
            run.assert_called_once()
        clicked.assert_not_called()
        self.w.send_ircc.assert_not_called()
        QTest.keyClick(self.w.power_button,Qt.Key_Return)
        self.w.send_ircc.assert_called_once_with('Confirm')

    def test_focus_action_is_unavailable_without_a_text_input_section(self):
        del self.w.text_input
        try:
            self.assertFalse(self.w._focused_shortcut_allowed('focus_text_input'))
        finally:
            self.w.text_input = self.editor
