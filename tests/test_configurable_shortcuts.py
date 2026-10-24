"""Configurable focused keys: validation, migration, Qt events and native suppression."""
import copy
import ctypes
import json
from pathlib import Path
import runpy
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent, QKeySequence, QShortcut
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
IS_TV = (ROOT / 'SonyRemote.pyw').exists()
REMOTE = runpy.run_path(str(ROOT / ('SonyRemote.pyw' if IS_TV else 'YamahaRemote.pyw')),
                        run_name='configurable_shortcuts_tests')
if IS_TV:
    import test_focused_shortcuts as fixture
else:
    import test_volume_controls as fixture


class ShortcutConfigurationTests(unittest.TestCase):
    def validate(self, values):
        return REMOTE['validate_config']({'Shortcuts': values}, allow_incomplete=True)

    def test_missing_actions_default_and_empty_bindings_disable(self):
        config = self.validate({'Mute': '', 'VolumeUp': []})
        self.assertEqual(config['Shortcuts']['Mute'], [])
        self.assertEqual(config['Shortcuts']['VolumeUp'], [])
        self.assertEqual(config['Shortcuts']['VolumeDown'], ['Volume Down'])

    def test_full_disable_and_independent_defaults(self):
        disabled = self.validate({action: [] for action in REMOTE['DEFAULT_SHORTCUTS']})
        self.assertEqual(REMOTE['_compile_shortcuts'](disabled), {})
        a, b = REMOTE['_default_config_copy'](), REMOTE['_default_config_copy']()
        a['Shortcuts']['Mute'].clear()
        self.assertTrue(b['Shortcuts']['Mute'])
        self.assertTrue(REMOTE['DEFAULT_SHORTCUTS']['Mute'])

    def test_invalid_bindings_and_unknown_actions_fail_explicitly(self):
        for value in (None, False, 12, 'A', [''], ['NoSuchKey'], ['Ctrl+'], ['Ctrl+K, Ctrl+C']):
            with self.subTest(value=value), self.assertRaises(REMOTE['ConfigError']):
                self.validate({'Mute': value})
        self.assertEqual(self.validate({'MadeUp': []})['Shortcuts']['MadeUp'], [])
        for name in ('', ' Guide', 'Guide '):
            with self.assertRaises(REMOTE['ConfigError']):
                self.validate({name: ['F1']})

    def test_conflicts_include_defaults_and_equivalent_spellings(self):
        for values in ({'Mute': ['Volume Up']}, {'Mute': ['Ctrl+A', 'ctrl+a']}):
            with self.subTest(values=values), self.assertRaisesRegex(REMOTE['ConfigError'], 'assigned more than once'):
                self.validate(values)

    def test_modifiers_named_keys_and_multiple_bindings_compile(self):
        config = self.validate({'VolumeUp': ['Ctrl+Alt+Up', 'F8'], 'Mute': ['Ctrl++']})
        mapping = REMOTE['_compile_shortcuts'](config)
        for text in ('Ctrl+Alt+Up', 'F8'):
            self.assertEqual(mapping[QKeySequence(text)[0].toCombined()], 'volume_up')
        self.assertEqual(mapping[QKeySequence('Ctrl++')[0].toCombined()], 'mute')
        self.assertIn('Ctrl+Alt+Up', REMOTE['_shortcut_tip'](config, 'volume_up'))
        self.assertEqual(REMOTE['_shortcut_tip'](self.validate({'Mute': []}), 'mute'), '')

    def test_loading_adds_missing_defaults_but_preserves_disabled_actions(self):
        load = REMOTE['load_config']
        for shortcuts in (None, {'Mute': ''}):
            with self.subTest(shortcuts=shortcuts), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'remote.json'
                document = {'Host': 'device.local'}
                if IS_TV:
                    document['PreSharedKey'] = 'test-only'
                if shortcuts is not None:
                    document['Shortcuts'] = shortcuts
                path.write_text(json.dumps(document), encoding='utf-8')
                with patch.dict(load.__globals__, CONFIG_PATH=path):
                    config = load()
                saved = json.loads(path.read_text())
                self.assertEqual(set(saved['Shortcuts']), set(REMOTE['DEFAULT_SHORTCUTS']))
                self.assertEqual(saved['Host'], 'device.local')
                self.assertEqual(saved['Shortcuts'], dict(REMOTE['DEFAULT_SHORTCUTS'], **(shortcuts or {})))
                if shortcuts is not None:
                    self.assertEqual(saved['Shortcuts']['Mute'], '')

    def test_bad_configuration_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'remote.json'
            text = json.dumps({'Shortcuts': {'Mute': ['NoSuchKey']}})
            path.write_text(text)
            with patch.dict(REMOTE['load_config'].__globals__, CONFIG_PATH=path), \
                 self.assertRaises(REMOTE['ConfigError']):
                REMOTE['load_config']()
            self.assertEqual(path.read_text(), text)


class FocusedBindingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (fixture.FocusedShortcutTests if IS_TV else fixture.VolumeControlTests).setUpClass()

    def setUp(self):
        if IS_TV:
            self.fixture = fixture.FocusedShortcutTests()
            self.fixture.setUp()
            self.window = self.fixture.window
            self.app = self.fixture.app
        else:
            self.fixture = fixture.VolumeControlTests()
            self.window = self.fixture.window()
            self.app = self.fixture.app
            self.window.connected = True
            self.window.last_status = {'power': 'on', 'input': 'tv', 'volume': 20, 'mute': False}
            self.window._last_status_at = time.monotonic()
            self.window.adjust_volume = Mock()
            self.window.toggle_mute = Mock()
            self.window._hotkey_ready.connect(self.window._drain_hotkeys, Qt.QueuedConnection)
            self.window.activateWindow()
            self.window.power_button.setFocus()
            self.app.processEvents()
        self.addCleanup(self.fixture.doCleanups)
        self.g = type(self.window).__init__.__globals__
        if not IS_TV:
            self.app.installEventFilter(self.window)
            self.addCleanup(lambda: self.app.removeEventFilter(self.window))

    def configure(self, **changes):
        raw = copy.deepcopy(self.window.config['Shortcuts'])
        raw.update(changes)
        self.window.config['Shortcuts'] = self.g['_validate_shortcuts'](raw)
        self.window._shortcut_bindings = self.g['_compile_shortcuts'](self.window.config)
        if IS_TV:
            self.window._publish_native_volume_state()

    def key(self, key, modifiers=Qt.NoModifier, *, kind=QEvent.KeyPress, repeat=False):
        event = QKeyEvent(kind, key, modifiers, '', repeat, 1)
        return self.window.eventFilter(self.window.power_button, event)

    def release(self, key, modifiers=Qt.NoModifier):
        return self.key(key, modifiers, kind=QEvent.KeyRelease)

    def test_additional_tv_commands_dispatch_and_unavailable_remain_inactive(self):
        self.window.api.remote_codes['Guide'] = 'test-guide'
        self.configure(Guide=['F1'], NotAdvertised=['F2'], volume_up=['F3'])
        self.assertTrue(self.key(Qt.Key_F1))
        self.release(Qt.Key_F1)
        self.window.send_ircc.assert_called_with('Guide')
        self.assertFalse(self.key(Qt.Key_F2))
        self.assertFalse(self.key(Qt.Key_F3))
        self.release(Qt.Key_F3)
        self.window.api.polling_responses = self.g['PollingResponses'](())
        report = self.window._cached_state()
        self.assertIn('NotAdvertised', report['hotkeys']['unavailable_tv_commands'])
        self.assertNotIn('Guide', report['hotkeys']['unavailable_tv_commands'])
        self.window.api.remote_codes['volume_up'] = 'test-raw-command'
        self.window.adjust_volume.reset_mock()
        self.assertTrue(self.key(Qt.Key_F3))
        self.release(Qt.Key_F3)
        self.window.send_ircc.assert_called_with('volume_up')
        self.window.adjust_volume.assert_not_called()
        del self.window.api.remote_codes['Guide']
        self.assertFalse(self.key(Qt.Key_F1))

    def test_unavailable_extra_command_does_not_own_native_volume_key(self):
        self.configure(VolumeUp=[], NotAdvertised=['Volume Up'])
        self.window._publish_native_volume_state()
        self.assertNotIn('ircc:NotAdvertised', self.window._native_bound_actions)
        self.window.api.remote_codes['NotAdvertised'] = 'test-command'
        self.window._publish_native_volume_state()
        self.assertIn('ircc:NotAdvertised', self.window._native_bound_actions)

    def test_custom_modified_volume_and_original_key_disabled(self):
        self.configure(VolumeUp=['Ctrl+Alt+Up'])
        self.assertFalse(self.key(Qt.Key_VolumeUp))
        self.assertTrue(self.key(Qt.Key_Up, Qt.ControlModifier | Qt.AltModifier))
        self.app.processEvents()
        self.window.adjust_volume.assert_called_once()
        self.assertEqual(self.window.adjust_volume.call_args.args[0], 1)
        self.release(Qt.Key_Up)

    def test_custom_mute_toggles_once_per_press(self):
        self.configure(Mute=['F9'])
        self.assertTrue(self.key(Qt.Key_F9))
        self.assertTrue(self.key(Qt.Key_F9, repeat=True))
        self.release(Qt.Key_F9)
        self.app.processEvents()
        self.window.toggle_mute.assert_called_once()

    def test_empty_volume_binding_passes_through(self):
        self.configure(VolumeUp=[])
        self.assertFalse(self.key(Qt.Key_VolumeUp))
        self.window.adjust_volume.assert_not_called()

    def test_popup_keeps_custom_navigation_keys(self):
        self.configure(VolumeUp=['Up']) if not IS_TV else self.configure(VolumeUp=['F8'])
        popup_key = Qt.Key_Up if not IS_TV else Qt.Key_F8
        with patch.object(QApplication, 'activePopupWidget', return_value=self.window.input_combo):
            self.assertFalse(self.key(popup_key))
        self.window.adjust_volume.assert_not_called()

    def test_native_modifier_resolution_and_disabled_physical_key(self):
        self.configure(VolumeUp=['Ctrl+Volume Up'])
        if not IS_TV:
            self.window._hotkey_state = (True, 123, time.monotonic(), False)
        self.assertIsNone(self.window._resolve_native_volume(0xAF, 0))
        self.assertEqual(self.window._resolve_native_volume(0xAF, Qt.ControlModifier.value), 'volume_up')
        self.configure(VolumeUp=[])
        self.assertIsNone(self.window._resolve_native_volume(0xAF, 0))

    def test_custom_bindings_keep_text_editing_or_global_defaults(self):
        if IS_TV:
            self.configure(VolumeUp=['A'])
            self.window.text_input.setFocus()
            self.app.processEvents()
            self.assertFalse(self.key(Qt.Key_A))
            self.window.adjust_volume.assert_not_called()
        else:
            self.configure(VolumeUp=['A'])
            self.window._hotkey_state = (True, 123, time.monotonic(), False)
            self.window._global_native_bindings = {(0xAF, 0): 'volume_up'}
            self.window._global_bindings = {Qt.Key_VolumeUp.value: 'volume_up'}
            self.window._hotkey_state = (True, 0, time.monotonic(), False)
            self.assertEqual(self.window._resolve_native_volume(0xAF, 0)[-1], 'volume_up')

    def test_smtc_keeps_custom_focused_playback(self):
        if not IS_TV:
            self.skipTest('SMTC is TV-only')
        self.configure(Next=['F9'])
        with patch.object(self.window, '_playback_key_mode', return_value='smtc'):
            self.assertTrue(self.key(Qt.Key_F9))
            self.release(Qt.Key_F9)
            self.assertFalse(self.key(Qt.Key_MediaNext))
        self.window.send_ircc.assert_called_once_with('Next')

    def test_native_press_retains_resolved_action_through_repeat_and_release(self):
        state = self.g['_VolumeKeyState']()
        accept = Mock(return_value=True)
        self.assertTrue(state.press(0xAD, 'volume_up', accept))
        self.assertTrue(state.press(0xAD, 'mute', accept))
        self.assertEqual([call.args[0] for call in accept.call_args_list], ['volume_up', 'volume_up'])
        self.assertTrue(state.release(0xAD))
        self.assertFalse(state.commands)

    def test_native_callback_keeps_owned_modifier_press_until_release(self):
        self.configure(VolumeUp=['Ctrl+Volume Up'])
        window = self.window
        if not IS_TV:
            window._global_bindings = {}
            window._publish_hotkey_state()
            # Exercise the Windows callback using an explicit foreground handle
            # even when this device-free suite runs on Linux.
            window._hotkey_state = (True, int(window.winId()), time.monotonic(), False)
        callback = window._accept_native_volume if IS_TV else window._accept_native_hotkey
        hook = self.g['_WindowsVolumeHook'](callback, resolve=window._resolve_native_volume)
        control_held = [True]
        hook._user32 = SimpleNamespace(
            GetForegroundWindow=lambda: int(window.winId()),
            GetAsyncKeyState=lambda key: 0x8000 if key == 0x11 and control_held[0] else 0,
            CallNextHookEx=Mock(return_value=17))
        hook._notify = Mock()
        data = self.g['_LowLevelKey'](0xAF, 0, 0, 0, 0)
        key = lambda message: hook._handle_key(0, message, ctypes.addressof(data))
        self.assertEqual(key(0x100), 1)
        control_held[0] = False
        self.assertEqual(key(0x100), 1)
        self.assertEqual(key(0x101), 1)
        self.app.processEvents()
        if not IS_TV:
            # The fake adjust action has no worker completion to drain the next
            # queued step; simulate that existing completion notification.
            window._schedule_hotkey_drain()
            self.app.processEvents()
        self.assertEqual(window.adjust_volume.call_count, 2)
        hook._user32.CallNextHookEx.assert_not_called()
        self.assertEqual(key(0x100), 17)
        self.assertEqual(key(0x101), 17)
        hook._notify.assert_not_called()

    def test_shifted_punctuation_uses_literal_binding(self):
        self.configure(Mute=['Ctrl++'])
        self.assertTrue(self.key(Qt.Key_Plus, Qt.ControlModifier | Qt.ShiftModifier))
        self.release(Qt.Key_Plus)
        self.app.processEvents()
        self.window.toggle_mute.assert_called_once()

    def test_real_qt_delivery_prevents_competing_shortcut(self):
        self.configure(VolumeUp=['Ctrl+Alt+Up'])
        shortcut = QShortcut(QKeySequence('Ctrl+Alt+Up'), self.window)
        activated = Mock()
        shortcut.activated.connect(activated)
        try:
            QTest.keyClick(self.window.power_button, Qt.Key_Up, Qt.ControlModifier | Qt.AltModifier)
            self.app.processEvents()
            self.window.adjust_volume.assert_called_once()
            activated.assert_not_called()
        finally:
            shortcut.deleteLater()

    def test_invalid_shortcut_edit_does_not_restart_running_window(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'remote.json'
            document = copy.deepcopy(self.window.config)
            document['Shortcuts']['Mute'] = ['NoSuchKey']
            path.write_text(json.dumps(document))
            before = copy.deepcopy(self.window.config['Shortcuts'])
            self.window._config_digest = b'previous-file'
            with patch.dict(self.g, CONFIG_PATH=path), \
                 patch.object(self.window, '_watch_config_paths'), \
                 patch.object(self.window, '_restart_for_config_change') as restart, \
                 patch.object(self.g['QMessageBox'], 'warning') as warning:
                self.window._check_external_config_change()
            restart.assert_not_called()
            warning.assert_called_once()
            self.assertIn('Shortcuts.Mute', warning.call_args.args[2])
            self.assertEqual(self.window.config['Shortcuts'], before)

    def test_press_state_reset_discards_captured_actions(self):
        state = self.g['_VolumeKeyState']()
        state.press(0xAF, 'volume_up', lambda action: True)
        state.clear()
        self.assertFalse(state.pressed)
        self.assertFalse(state.commands)
