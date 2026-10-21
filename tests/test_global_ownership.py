"""Per-binding IPC ownership and SMTC reservation, without device requests."""
import json
import time
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QKeyEvent
import test_focused_shortcuts as fixture


class BindingOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FocusedShortcutTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.FocusedShortcutTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w = self.fixture.window
        self.g = type(self.w).__init__.__globals__
        self.app = self.fixture.app
        self.state = json.loads((Path(__file__).parent/'fixtures/amplifier_protocol_v1.json').read_text())['states'][0]
        self.state['global_shortcuts'] = [Qt.Key_F8.value]
        self.w.amplifier_state = self.state
        self.w.amplifier_client.state = self.state
        self.w.amplifier_client.state_deadline = time.monotonic()+6

    def event(self,key,modifiers=Qt.NoModifier):
        return QKeyEvent(QEvent.KeyPress,key,modifiers)

    def test_yield_only_overlapping_key_not_all_volume_actions(self):
        self.w.config['Shortcuts']['VolumeUp'] = ['F8','F9']
        self.w._shortcut_bindings = self.g['_compile_shortcuts'](self.w.config)
        self.assertIsNone(self.w._qt_shortcut_command(self.event(Qt.Key_F8)))
        self.assertEqual(self.w._qt_shortcut_command(self.event(Qt.Key_F9)),'volume_up')
        self.assertEqual(self.w._qt_shortcut_command(self.event(Qt.Key_VolumeDown)),'volume_down')

    def test_global_volume_binding_wins_over_focused_navigation_or_playback(self):
        self.state['global_shortcuts'] = [Qt.Key_Up.value,Qt.Key_MediaNext.value]
        self.assertIsNone(self.w._qt_shortcut_command(self.event(Qt.Key_Up)))
        self.assertIsNone(self.w._qt_shortcut_command(self.event(Qt.Key_MediaNext)))
        self.assertEqual(self.w._qt_shortcut_command(self.event(Qt.Key_Down)),'Down')

    def test_modifiers_are_separate_and_expiry_restores_focused_binding(self):
        self.state['global_shortcuts'] = [Qt.Key_Up.value | Qt.ControlModifier.value]
        self.assertEqual(self.w._qt_shortcut_command(self.event(Qt.Key_Up)),'Up')
        self.assertIsNone(self.w._qt_shortcut_command(self.event(Qt.Key_Up,Qt.ControlModifier)))
        self.w.amplifier_client.state_deadline = time.monotonic()-1
        self.assertFalse(self.w._amplifier_owns_hotkeys(Qt.Key_Up.value | Qt.ControlModifier.value))

    def test_offline_standby_or_disabled_owner_does_not_block(self):
        for update in ({'online':False},{'power':'standby'},{'global_hotkeys_active':False}):
            with self.subTest(update=update),patch.dict(self.state,update):
                self.assertFalse(self.w._amplifier_owns_hotkeys(Qt.Key_F8.value))

    def test_native_volume_resolver_yields_only_actual_conflict(self):
        self.state['global_shortcuts'] = [Qt.Key_VolumeUp.value]
        self.w._publish_native_volume_state()
        self.assertIsNone(self.w._resolve_native_volume(0xAF,0))
        self.assertEqual(self.w._resolve_native_volume(0xAE,0),'volume_down')
        self.w._native_volume_state = (int(self.w.winId()),False,time.monotonic()-1)
        self.assertEqual(self.w._resolve_native_volume(0xAF,0),'volume_up')

    def test_legacy_producer_owns_only_physical_volume(self):
        del self.state['global_shortcuts']
        self.assertTrue(self.w._amplifier_owns_hotkeys(Qt.Key_VolumeUp.value))
        self.assertFalse(self.w._amplifier_owns_hotkeys(Qt.Key_F8.value))

    def test_invalid_ownership_payload_rejected(self):
        validate = self.g['AmplifierClient']._validate_state
        for value in ('F8',[True],[1,1],[-1],[0],[1]*129):
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate(dict(self.state,global_shortcuts=value))
        validate(self.state)

    def test_smtc_withdraws_only_actions_owned_by_global_bindings(self):
        from types import SimpleNamespace
        self.w.config['SMTCPlaybackKeys'] = True
        self.w._smtc_failed = False
        self.w._smtc = Mock()
        self.w.last_power = 'active'
        self.state['global_shortcuts'] = [Qt.Key_MediaNext.value]
        with patch.dict(self.g,sys=SimpleNamespace(platform='win32')):
            self.w._refresh_playback_routing()
            commands = self.w._smtc.update.call_args.args[0]
            self.assertNotIn('Next',commands)
            self.assertIn('Prev',commands)
            self.state['global_shortcuts'] = [Qt.Key_MediaTogglePlayPause.value]
            commands = self.w._smtc_commands()
            self.assertIn('Next',commands)
            self.assertNotIn('Play',commands)
            self.assertNotIn('Pause',commands)
            self.state['global_shortcuts'] = []
            self.w._refresh_playback_routing()
            self.assertIn('Play',self.w._smtc.update.call_args.args[0])
            self.assertIn('Next',self.w._smtc.update.call_args.args[0])

    def test_smtc_reserves_transport_but_custom_volume_key_playback_works(self):
        self.w.config['SMTCPlaybackKeys'] = True
        self.w._smtc_failed = False
        self.w._smtc = Mock()
        self.state['global_shortcuts'] = []
        self.w.config['Shortcuts']['VolumeUp'] = []
        self.w.config['Shortcuts']['Next'] = ['Volume Up']
        self.w._shortcut_bindings = self.g['_compile_shortcuts'](self.w.config)
        self.w._refresh_playback_routing()
        self.assertIsNone(self.w._qt_shortcut_command(self.event(Qt.Key_MediaNext)))
        self.assertEqual(self.w._resolve_native_volume(0xAF,0),'Next')
