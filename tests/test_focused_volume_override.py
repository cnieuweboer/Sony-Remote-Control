"""Focused TV handling is prepared before readiness is sent to the amplifier."""
import json
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import Qt
from PySide6.QtNetwork import QLocalSocket
import test_focused_shortcuts as fixture


class FocusedOverrideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.FocusedShortcutTests.setUpClass()

    def setUp(self):
        self.fixture = fixture.FocusedShortcutTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.w, self.app = self.fixture.window,self.fixture.app
        self.g = type(self.w).__init__.__globals__
        self.state = json.loads((Path(__file__).parent/'fixtures/amplifier_protocol_v1.json').read_text())['states'][0]
        self.state.update(input='pc',global_shortcuts=[Qt.Key_VolumeUp.value],focused_volume_override_supported=True)
        self.w.config['AmplifierTVInput'] = 'tv'
        self.w.amplifier_state = self.w.amplifier_client.state = self.state
        self.w.amplifier_client.state_deadline = time.monotonic()+6
        self.w._status_observed_at = time.monotonic()
        self.w.last_power = 'active'
        self.w.last_volume = {'volume':20,'mute':False}
        self.w._native_volume_hook = SimpleNamespace(installed=True,stop=Mock())
        self.w._publish_native_volume_state()

    def test_prepares_local_handler_before_announcing_readiness(self):
        self.assertFalse(self.w._amplifier_owns_hotkeys(Qt.Key_VolumeUp.value,focused=True))
        self.assertEqual(self.w._resolve_native_volume(0xAF,0),'volume_up')
        self.assertTrue(self.w._amplifier_owns_hotkeys(Qt.Key_VolumeUp.value))
        self.assertFalse(self.w._use_amplifier())
        def announce(*args):
            self.assertEqual(self.w._resolve_native_volume(0xAF,0),'volume_up')
        with patch.object(self.w.amplifier_client,'set_focused_volume',side_effect=announce) as send:
            self.w._publish_native_volume_state()
            send.assert_called_once()

    def test_tv_input_restores_amp_route_and_global_ownership(self):
        self.state['input'] = 'tv'
        self.w._publish_native_volume_state()
        self.assertTrue(self.w._use_amplifier())
        self.assertTrue(self.w._amplifier_owns_hotkeys(Qt.Key_VolumeUp.value,focused=True))
        self.assertIsNone(self.w._resolve_native_volume(0xAF,0))

    def test_not_ready_off_tv_popup_editing_or_no_input_does_not_request(self):
        for attr,value in (('last_power','standby'),('last_volume',None),('initialized',False),
                           ('calibration_active',True)):
            with self.subTest(attr=attr),patch.object(self.w,attr,value):
                self.assertEqual(self.w._focused_tv_volume_bindings(),[])
        with patch.object(self.w,'isActiveWindow',return_value=False):
            self.assertEqual(self.w._focused_tv_volume_bindings(),[])
        with patch.object(self.g['QApplication'],'activePopupWidget',return_value=Mock()):
            self.assertEqual(self.w._focused_tv_volume_bindings(),[])
        self.w.config['AmplifierTVInput'] = ''
        self.assertEqual(self.w._focused_tv_volume_bindings(),[])

    def test_global_nonvolume_conflicts_do_not_get_focus_exception(self):
        self.w._shortcut_bindings[Qt.Key_A.value] = 'Next'
        self.assertNotIn(Qt.Key_A.value,self.w._focused_tv_volume_bindings())
        self.w._shortcut_bindings[Qt.Key_MediaNext.value] = 'volume_up'
        self.w.config['SMTCPlaybackKeys'] = True
        self.assertNotIn(Qt.Key_MediaNext.value,self.w._focused_tv_volume_bindings())

    def test_physical_volume_requires_working_native_hook_on_windows(self):
        self.w._shortcut_bindings[Qt.Key_A.value] = 'volume_up'
        self.w._native_volume_hook.installed = False
        with patch.dict(self.g,sys=SimpleNamespace(platform='win32')):
            candidates = self.w._focused_tv_volume_bindings()
            self.assertNotIn(Qt.Key_VolumeUp.value,candidates)
            self.assertIn(Qt.Key_A.value,candidates)

    def test_stale_or_unsupported_state_restores_global_priority(self):
        self.w._status_observed_at = time.monotonic()-7
        self.assertTrue(self.w._amplifier_owns_hotkeys(Qt.Key_VolumeUp.value,focused=True))
        self.w._native_tv_override = (time.monotonic()-1,[Qt.Key_VolumeUp.value])
        self.assertIsNone(self.w._resolve_native_volume(0xAF,0))
        self.w._status_observed_at = time.monotonic()
        del self.state['focused_volume_override_supported']
        self.assertEqual(self.w._focused_tv_override_bindings(),[])

    def test_context_is_event_driven_and_old_servers_are_not_sent_new_messages(self):
        client = self.g['AmplifierClient']()
        self.addCleanup(client.stop)
        client.state = self.state
        client.state_deadline = time.monotonic()+6
        observed = time.monotonic()
        with patch.object(client.socket,'state',return_value=QLocalSocket.ConnectedState), \
             patch.object(client.socket,'bytesToWrite',return_value=0), \
             patch.object(client.socket,'write',side_effect=lambda data:len(data)) as write:
            client.set_focused_volume(123,[Qt.Key_VolumeUp.value],'tv',observed)
            client.set_focused_volume(123,[Qt.Key_VolumeUp.value],'tv',observed)
            self.assertEqual(write.call_count,1)
            client.set_focused_volume(123,[Qt.Key_VolumeUp.value],'tv',observed+1)
            self.assertEqual(write.call_count,2)
            client.set_focused_volume(0,[],'tv',observed+1)
            self.assertEqual(json.loads(write.call_args.args[0])['window'],0)
            del self.state['focused_volume_override_supported']
            client.set_focused_volume(123,[Qt.Key_VolumeUp.value],'tv',observed+2)
            self.assertEqual(write.call_count,3)

    def test_invalid_capability_rejected(self):
        validate = self.g['AmplifierClient']._validate_state
        for value in (1,[],None,'true'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                validate(dict(self.state,focused_volume_override_supported=value))

    def test_literal_punctuation_and_keypad_enter_ownership_aliases(self):
        owned = self.g['_binding_owned']
        self.assertTrue(owned([Qt.Key_Plus.value|Qt.ControlModifier.value],
                             Qt.Key_Plus.value|Qt.ControlModifier.value|Qt.ShiftModifier.value))
        self.assertTrue(owned([Qt.Key_Enter.value],Qt.Key_Enter.value|Qt.KeypadModifier.value))
