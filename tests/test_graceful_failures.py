"""Offscreen UI checks for independently available TV discovery features."""
from collections import Counter
import json
import os
from pathlib import Path
import runpy
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'SonyRemote.pyw'), run_name='discovery_tests')
GLOBALS = REMOTE['SonyRemote'].initialize.__globals__


class Device:
    def __init__(self):
        self.calls = Counter()
        self.failures = {}
        self.holds = {}
        self.results = {}
        self.remote_codes = {}
        self.power = 'active'

    def request(self, feature):
        self.calls[feature] += 1
        if feature in self.holds and not self.holds[feature].wait(3):
            raise TimeoutError('Fake discovery did not get released')
        if feature in self.failures:
            raise self.failures[feature]

    def load_remote_codes(self):
        self.request('remote_codes')
        self.remote_codes = {name: name for name in ('Up', 'Down', 'Left', 'Right', 'Confirm',
                                                    'Return', 'Home', 'Settings', 'TvInput', 'Num1', 'PlayPause')}

    def get_apps(self):
        self.request('apps')
        return self.results.get('apps', [{'uri': 'app', 'title': 'App'}])

    def get_inputs(self):
        self.request('inputs')
        return self.results.get('inputs', [{'uri': 'input', 'title': 'Input', 'status': 'true'}])

    def get_power(self):
        self.request('power')
        return self.power

    def get_volume(self):
        self.request('volume')
        return {'volume': 20, 'mute': False, 'minVolume': 0, 'maxVolume': 100}

    def resolve_remote_command(self, name):
        return REMOTE['SonyApi'].resolve_remote_command(self, name)


class DiscoveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        config = REMOTE['_default_config_copy']()
        config.update(Host='127.0.0.1', PreSharedKey='test-only', Mica=False, DarkMode=False,
                      InputControls=True, SystemTray=False, CloseToTray=False)
        for collection, uri, title in (('Apps', 'app', 'App'), ('Inputs', 'input', 'Input')):
            config[collection] = {uri: {'Name': title, 'NameOverride': '', 'Hidden': False, 'Favorite': True}}
        path = Path(self.folder.name) / 'SonyRemote.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        self.context = patch.dict(GLOBALS, {'CONFIG_PATH': path})
        self.context.start()
        self.addCleanup(self.context.stop)
        self.device = Device()
        # Tests neither bind production IPC names nor connect to another app.
        with patch.object(REMOTE['AmplifierClient'], 'start', lambda client: None), \
             patch.object(REMOTE['SonyRemote'], '_setup_calibration', lambda window: None), \
             patch.object(REMOTE['SonyRemote'], '_setup_activation', lambda window: None), \
             patch.object(REMOTE['SonyRemote'], '_install_windows_volume_hook', lambda window: None):
            self.window = REMOTE['SonyRemote'](config)
        self.window.api = self.device
        self.window.pool.setMaxThreadCount(6)
        self.window.show_error = Mock()
        self.addCleanup(self.shutdown)

    def shutdown(self):
        for hold in self.device.holds.values():
            hold.set()
        self.window._shutdown()
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.fail('Timed out waiting for fake device/UI result')

    def start_and_settle(self):
        self.window.show()
        self.wait(lambda: self.window.initialized and not self.window._discovery_pending
                  and self.window._discovery_ready | self.window._discovery_errors.keys()
                  == {'remote_codes', 'apps', 'inputs'})
        self.window.poll_timer.stop()

    def buttons(self, command):
        return [button for button in self.window.findChildren(QPushButton)
                if button.property('remoteCommand') == command]

    def test_app_failure_preserves_power_volume_navigation_and_inputs(self):
        self.device.failures['apps'] = REMOTE['DeviceResponseError']('Application list unavailable')
        self.start_and_settle()
        self.assertEqual(self.window.power_button.text(), 'Standby')
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.buttons('Up')[0].isEnabled())
        self.assertTrue(self.window.input_combo.isEnabled())
        self.assertFalse(self.window.app_combo.isEnabled())
        self.assertFalse(self.window.app_favorites_layout.itemAt(0).widget().isEnabled())
        self.assertIn('Apps unavailable', self.window.power_button.toolTip())
        self.window.show_error.assert_not_called()

    def test_remote_code_failure_disables_only_ircc_controls(self):
        self.device.failures['remote_codes'] = REMOTE['SonyConnectionError']('Remote code discovery unavailable')
        self.start_and_settle()
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.window.app_combo.isEnabled())
        self.assertTrue(self.window.input_combo.isEnabled())
        self.assertTrue(self.window.text_input.isEnabled())
        for button in self.window.findChildren(QPushButton):
            if button.property('remoteCommand') is not None:
                self.assertFalse(button.isEnabled())
        self.window.show_error.assert_not_called()

    def test_input_failure_disables_input_selection_and_input_favorites_only(self):
        self.device.failures['inputs'] = REMOTE['DeviceResponseError']('Input list unavailable')
        self.start_and_settle()
        self.assertFalse(self.window.input_combo.isEnabled())
        self.assertFalse(self.window.input_favorites_layout.itemAt(0).widget().isEnabled())
        self.assertTrue(self.window.app_combo.isEnabled())
        self.assertTrue(self.buttons('TvInput')[0].isEnabled())  # IRCC input menu is independent.
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.window.show_error.assert_not_called()

    def test_blocked_discovery_does_not_hold_up_status_or_other_discovery(self):
        self.device.holds['apps'] = threading.Event()
        self.window.show()
        self.wait(lambda: self.window.initialized
                  and {'remote_codes', 'inputs'} <= self.window._discovery_ready)
        self.assertIn('apps', self.window._discovery_pending)
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.window.input_combo.isEnabled())
        self.assertFalse(self.window.app_combo.isEnabled())
        self.window.initialize()
        self.assertEqual(self.device.calls['apps'], 1)
        self.device.holds['apps'].set()
        self.wait(lambda: 'apps' in self.window._discovery_ready)
        self.assertTrue(self.window.app_combo.isEnabled())

    def test_blocked_input_discovery_does_not_hold_up_core_status(self):
        self.device.holds['inputs'] = threading.Event()
        self.window.show()
        self.wait(lambda: self.window.initialized
                  and {'remote_codes', 'apps'} <= self.window._discovery_ready)
        self.assertIn('inputs', self.window._discovery_pending)
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.window.app_combo.isEnabled())
        self.assertFalse(self.window.input_combo.isEnabled())
        self.assertEqual(self.device.calls['inputs'], 1)
        self.device.holds['inputs'].set()
        self.wait(lambda: 'inputs' in self.window._discovery_ready)
        self.assertTrue(self.window.input_combo.isEnabled())

    def test_blocked_input_status_refresh_does_not_block_power_polling(self):
        self.start_and_settle()
        self.device.holds['inputs'] = threading.Event()
        self.window._poll_input_status()
        self.wait(lambda: self.window._input_status_running)
        before = self.device.calls['power']
        self.window.poll_status()
        self.wait(lambda: self.device.calls['power'] > before and not self.window.poll_running)
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.input_combo.isEnabled())
        self.device.holds['inputs'].set()
        self.wait(lambda: not self.window._input_status_running)

    def test_existing_status_timer_retries_only_failed_discovery_and_restores_controls(self):
        self.device.failures['apps'] = REMOTE['DeviceResponseError']('Temporary failure')
        self.start_and_settle()
        before = self.device.calls.copy()
        self.device.failures.clear()
        self.window.poll_timer.start(20)
        self.wait(lambda: 'apps' in self.window._discovery_ready)
        self.window.poll_timer.stop()
        self.assertEqual(self.device.calls['remote_codes'], before['remote_codes'])
        self.assertEqual(self.device.calls['apps'], before['apps'] + 1)
        self.assertTrue(self.window.app_combo.isEnabled())
        self.assertTrue(self.window.app_favorites_layout.itemAt(0).widget().isEnabled())
        self.assertEqual(self.window.app_combo.toolTip(), '')
        self.assertNotIn('apps', self.window._discovery_errors)

    def test_all_discovery_failures_still_allow_core_tv_controls(self):
        for feature in ('remote_codes', 'apps', 'inputs'):
            self.device.failures[feature] = REMOTE['DeviceResponseError']('Unavailable')
        self.start_and_settle()
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.window.text_input.isEnabled())
        self.assertFalse(self.window.app_combo.isEnabled())
        self.assertFalse(self.window.input_combo.isEnabled())
        self.assertFalse(self.buttons('Up')[0].isEnabled())
        self.window.show_error.assert_not_called()

    def test_real_connection_failure_still_disables_core_controls(self):
        self.device.failures['power'] = REMOTE['SonyConnectionError']('TV unreachable')
        self.window.show()
        self.wait(lambda: self.window.power_button.text() == 'Offline'
                  and not self.window._discovery_pending)
        self.assertFalse(self.window.power_button.isEnabled())
        self.assertFalse(self.window.volume_up.isEnabled())
        self.assertFalse(self.window.app_combo.isEnabled())
        self.window.show_error.assert_not_called()

    def test_tv_standby_hides_amplifier_volume_and_mute_until_tv_wakes(self):
        self.start_and_settle()
        state = json.loads((ROOT / 'tests/fixtures/amplifier_protocol_v1.json').read_text())['states'][0]
        state.update(online=True, power='on', input='tv', raw_volume=52, mute=True,
                     volume_display=52, volume_unit='raw')
        with patch.object(self.window, '_use_amplifier', return_value=True), \
                patch.object(self.window, '_matched_volume_mapping', return_value=None):
            self.window._amplifier_state_changed(state)
            self.assertEqual(self.window.volume_value.text(), '52')
            self.assertEqual(self.window.mute_button.text(), 'Unmute')
            self.assertTrue(self.window.volume_up.isEnabled())
            self.device.power = 'standby'
            self.window.poll_status()
            self.wait(lambda: self.window.last_power == 'standby' and not self.window.poll_running)
            for widget in (self.window.volume_up, self.window.volume_down, self.window.mute_button):
                self.assertFalse(widget.isEnabled())
            self.assertEqual(self.window.volume_value.text(), '—')
            self.assertEqual(self.window.mute_button.text(), 'Mute')
            # Repeated amp IPC updates must not reveal the cached active volume/mute.
            self.window._amplifier_state_changed(dict(state, raw_volume=53, volume_display=53))
            self.assertEqual(self.window.volume_value.text(), '—')
            self.assertEqual(self.window.mute_button.text(), 'Mute')
            self.assertFalse(self.window.mute_button.isEnabled())
            self.device.power = 'active'
            self.window.poll_status()
            self.wait(lambda: self.window.last_power == 'active' and not self.window.poll_running)
            self.assertTrue(self.window.volume_up.isEnabled())
            self.assertTrue(self.window.mute_button.isEnabled())
            self.assertEqual(self.window.volume_value.text(), '53')
            self.assertEqual(self.window.mute_button.text(), 'Unmute')

    def test_offline_tv_keeps_amplifier_controls_disabled_and_display_empty(self):
        self.start_and_settle()
        self.window.last_volume = {'volume': 20, 'mute': True}
        with patch.object(self.window, '_use_amplifier', return_value=True):
            self.window.set_offline()
            self.window.update_volume_ui({'volume': 60, 'mute': True})
            self.assertEqual(self.window.volume_value.text(), '—')
            self.assertEqual(self.window.mute_button.text(), 'Mute')
            self.assertFalse(self.window.volume_up.isEnabled())
            self.assertFalse(self.window.volume_down.isEnabled())
            self.assertFalse(self.window.mute_button.isEnabled())

    def test_calibrated_volume_shows_bucket_decimals_and_integer_first_step(self):
        self.start_and_settle()
        document = {"format_version": 2, "tv_device_id": "tv-id", "amplifier_device_id": "amp-id",
                    "amplifier_tv_input": "tv",
                    "tv_volume_by_amplifier_raw_volume": [19] * 51 + [20, 20, 20, 21],
                    "estimated_amplifier_raw_volumes": [53]}
        mapping = REMOTE['prepare_volume_mapping'](document)
        with patch.object(self.window, '_use_amplifier', return_value=True), \
                patch.object(self.window, '_matched_volume_mapping', return_value=mapping):
            for raw, expected in ((51, '20'), (52, '20.3'), (53, '≈20.7'), (54, '21')):
                self.window.amplifier_state = {'raw_volume': raw, 'mute': False}
                self.window.update_volume_ui({'volume': 20, 'mute': False})
                self.assertEqual(self.window.volume_value.text(), expected)
                self.assertEqual(self.window.volume_value.toolTip(), 'Calibrated TV scale')

    def test_remote_codes_recovery_restores_navigation_and_shortcut_capabilities(self):
        self.device.failures['remote_codes'] = REMOTE['DeviceResponseError']('Unavailable')
        self.start_and_settle()
        self.assertFalse(self.buttons('Up')[0].isEnabled())
        self.device.failures.clear()
        self.window.initialize()
        self.wait(lambda: 'remote_codes' in self.window._discovery_ready)
        self.assertTrue(self.buttons('Up')[0].isEnabled())
        self.assertTrue(self.buttons('Settings')[0].isEnabled())
        self.assertTrue(self.window._focused_shortcut_allowed('Up'))

    def test_input_recovery_restores_dropdown_and_favorites(self):
        self.device.failures['inputs'] = REMOTE['DeviceResponseError']('Unavailable')
        self.start_and_settle()
        self.device.failures.clear()
        self.window.initialize()
        self.wait(lambda: 'inputs' in self.window._discovery_ready)
        self.assertTrue(self.window.input_combo.isEnabled())
        self.assertTrue(self.window.input_favorites_layout.itemAt(0).widget().isEnabled())
        self.assertEqual(self.window.input_combo.currentData(), 'input')

    def test_amplifier_feedback_survives_tv_poll_and_clears_on_accepted_ack(self):
        self.start_and_settle()
        client = self.window.amplifier_client
        client.command_failed.emit('Command acknowledgement timed out.', 'timeout')
        self.wait(lambda: not self.window.poll_running)
        previous_sequence = self.window._status_sequence
        self.window.poll_status()
        self.wait(lambda: self.window._status_sequence > previous_sequence)
        self.assertIn('Command acknowledgement timed out.', self.window.power_button.toolTip())
        timer = Mock()
        client.pending[1] = timer
        with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                        _ipc_frames=lambda socket, buffer: [
                            {'version': 1, 'type': 'ack', 'id': 1, 'accepted': True}]):
            client._read()
        self.assertNotIn('Command acknowledgement timed out.', self.window.power_button.toolTip())
        timer.stop.assert_called_once()
        self.window.show_error.assert_not_called()

    def test_connection_and_state_protocol_feedback_clear_on_fresh_amplifier_state(self):
        self.start_and_settle()
        client = self.window.amplifier_client
        state = json.loads((ROOT / 'tests/fixtures/amplifier_protocol_v1.json').read_text())['states'][0]
        with patch.object(client, 'send_command') as send:
            for category in ('connection', 'timeout', 'rejection', 'protocol', 'protocol_state', 'protocol_ack'):
                with self.subTest(category=category):
                    # Identical wording proves recovery uses the category.
                    client.command_failed.emit('Previous amplifier error.', category)
                    with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                                    _ipc_frames=lambda socket, buffer: [state]):
                        client._read()
                    text = self.window.power_button.toolTip()
                    self.assertEqual('Previous amplifier error.' in text, category not in ('connection', 'protocol_state'))
            send.assert_not_called()
        client.command_succeeded.emit()
        self.assertNotIn('Previous amplifier error.', self.window.power_button.toolTip())
        self.window.show_error.assert_not_called()

    def test_pending_ack_clears_only_ack_protocol_feedback_without_sending(self):
        self.start_and_settle()
        client = self.window.amplifier_client
        with patch.object(client, 'send_command') as send:
            for category in ('protocol_ack', 'protocol_state', 'protocol', 'timeout', 'rejection'):
                client.command_failed.emit('Previous amplifier error.', category)
                client.pending[1] = Mock()
                with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                                _ipc_frames=lambda socket, buffer: [
                                    {'version': 1, 'type': 'ack', 'id': 1, 'accepted': False, 'reason': 'queue_full'}]):
                    client._read()
                self.assertEqual('Previous amplifier error.' in self.window.power_button.toolTip(),
                                 category != 'protocol_ack')
            client.command_failed.emit('Previous amplifier error.', 'protocol_ack')
            with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                            _ipc_frames=lambda socket, buffer: [
                                {'version': 1, 'type': 'ack', 'id': 999, 'accepted': True}]):
                client._read()
            self.assertIn('Previous amplifier error.', self.window.power_button.toolTip())
            send.assert_not_called()

    def test_expired_or_invalid_state_does_not_clear_connection_feedback(self):
        self.start_and_settle()
        client = self.window.amplifier_client
        state = json.loads((ROOT / 'tests/fixtures/amplifier_protocol_v1.json').read_text())['states'][0]
        client.command_failed.emit('Connection lost.', 'connection')
        with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                        _ipc_frames=lambda socket, buffer: [dict(state, valid_for_ms=0)]):
            client._read()
        self.assertIn('Connection lost.', self.window.power_button.toolTip())
        # A malformed state gets protocol feedback, rather than clearing errors.
        with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                        _ipc_frames=lambda socket, buffer: [dict(state, mute='invalid')]), \
             patch.dict(REMOTE['record_exception'].__globals__, record_exception=Mock()):
            client._read()
        self.assertIn('Incompatible amplifier protocol', self.window.power_button.toolTip())
        self.window.show_error.assert_not_called()

    def test_pause_and_resume_reset_pending_discovery_and_retry_unfinished_feature(self):
        self.start_and_settle()
        self.device.holds['apps'] = threading.Event()
        self.window._discovery_ready.discard('apps')
        before = self.device.calls['apps']
        self.window.initialize()
        self.wait(lambda: self.device.calls['apps'] > before)
        release = threading.Timer(0.02, self.device.holds['apps'].set)
        release.start()
        timers = self.window._pause_background_work()
        release.join()
        self.assertFalse(self.window._discovery_pending)
        self.assertFalse(self.window._input_status_running)
        with patch.object(REMOTE['AmplifierClient'], 'start', lambda client: None), \
             patch.object(REMOTE['SonyRemote'], '_setup_calibration', lambda window: None), \
             patch.object(REMOTE['SonyRemote'], '_setup_activation', lambda window: None):
            self.window._resume_background_work(timers)
        self.wait(lambda: 'apps' in self.window._discovery_ready and not self.window._discovery_pending)
        self.assertTrue(self.window.app_combo.isEnabled())
        self.assertTrue(self.window.power_button.isEnabled())

    def test_late_discovery_success_cannot_reenable_controls_after_connection_loss(self):
        self.device.holds['apps'] = threading.Event()
        self.window.show()
        self.wait(lambda: self.window.initialized and 'apps' in self.window._discovery_pending)
        self.device.failures['power'] = REMOTE['SonyConnectionError']('Connection lost')
        self.wait(lambda: not self.window.poll_running)
        self.window.poll_status()
        self.wait(lambda: self.window.last_power is None)
        self.device.holds['apps'].set()
        self.wait(lambda: 'apps' in self.window._discovery_ready)
        self.assertFalse(self.window.power_button.isEnabled())
        self.assertFalse(self.window.app_combo.isEnabled())
        self.assertFalse(self.window.volume_up.isEnabled())

    def test_malformed_optional_result_does_not_break_other_controls(self):
        self.device.results['apps'] = None
        self.start_and_settle()
        self.assertFalse(self.window.app_combo.isEnabled())
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertTrue(self.window.input_combo.isEnabled())
        self.window.show_error.assert_not_called()

    def test_standby_keeps_power_and_successful_app_launcher_available(self):
        self.device.power = 'standby'
        self.start_and_settle()
        self.assertEqual(self.window.power_button.text(), 'Power On')
        self.assertTrue(self.window.power_button.isEnabled())
        self.assertTrue(self.window.app_combo.isEnabled())
        self.assertFalse(self.window.volume_up.isEnabled())
        self.assertFalse(self.window.input_combo.isEnabled())
        self.assertFalse(self.buttons('Up')[0].isEnabled())

    def test_unadvertised_ircc_command_stays_disabled_after_successful_discovery(self):
        self.start_and_settle()
        self.assertTrue(self.buttons('PlayPause')[0].isEnabled())
        self.assertFalse(self.buttons('Rewind')[0].isEnabled())
        self.assertFalse(self.window.channel_buttons['Num2'].isEnabled())

    def settle_volume_controls(self):
        self.start_and_settle()
        self.wait(lambda: not self.window._workers and not self.window.poll_running)

    def pending_poll_result(self):
        jobs = []
        with patch.object(self.window, 'run_worker', side_effect=lambda *args: jobs.append(args)):
            self.window.poll_status()
        query, done, failed, finished = jobs[0]
        data = query()
        def deliver():
            done(data)
            finished()
        return deliver

    def feed_amplifier_state(self, **changes):
        self.window.config['AmplifierTVInput'] = 'tv'
        state = json.loads((ROOT / 'tests/fixtures/amplifier_protocol_v1.json').read_text())['states'][0]
        state.update(changes)
        with patch.dict(REMOTE['AmplifierClient']._read.__globals__,
                        _ipc_frames=lambda socket, buffer: [state]):
            self.window.amplifier_client._read()
        return state

    def test_amplifier_routing_skips_tv_audio_but_keeps_power_and_input_reads(self):
        self.settle_volume_controls()
        self.feed_amplifier_state()
        self.assertTrue(self.window._amplifier_routed)
        power_before, inputs_before = self.device.calls['power'], self.device.calls['inputs']
        self.device.get_volume = Mock()
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running and not self.window._input_status_running)
        self.device.get_volume.assert_not_called()
        self.assertGreater(self.device.calls['power'], power_before)
        self.assertGreater(self.device.calls['inputs'], inputs_before)
        self.assertEqual(self.window.volume_value.text(), '100')

    def test_amplifier_disconnect_immediately_refreshes_tv_audio_before_enabling_controls(self):
        self.settle_volume_controls()
        self.feed_amplifier_state()
        self.device.get_volume = Mock(return_value={'volume': 35, 'mute': True})
        self.window.amplifier_client._expire()
        self.assertTrue(self.window.poll_running)
        self.assertIsNone(self.window.last_volume)
        self.assertEqual(self.window.volume_value.text(), '—')
        self.assertFalse(self.window.volume_up.isEnabled())
        self.wait(lambda: not self.window.poll_running and self.device.get_volume.called)
        self.device.get_volume.assert_called_once()
        self.assertEqual(self.window.volume_value.text(), '35')
        self.assertEqual(self.window.mute_button.text(), 'Unmute')
        self.assertTrue(self.window.volume_up.isEnabled())
        self.assertFalse(self.window.polling.pending)

    def test_fallback_during_an_existing_poll_refreshes_as_soon_as_it_finishes(self):
        self.settle_volume_controls()
        self.feed_amplifier_state()
        deliver = self.pending_poll_result()  # Captures power only while routed.
        self.device.get_volume = Mock(return_value={'volume': 36, 'mute': False})
        self.feed_amplifier_state(input='hdmi2')
        self.assertTrue(self.window.polling.pending)
        self.device.get_volume.assert_not_called()
        deliver()
        self.wait(lambda: self.device.get_volume.called and not self.window.poll_running)
        self.device.get_volume.assert_called_once()
        self.assertEqual(self.window.volume_value.text(), '36')

    def test_inflight_audio_from_before_amplifier_routing_cannot_replace_routed_display(self):
        self.settle_volume_controls()
        observed_at = self.window._volume_observed_at
        deliver = self.pending_poll_result()
        self.feed_amplifier_state()
        deliver()
        self.assertEqual(self.window._volume_observed_at, observed_at)
        self.assertEqual(self.window.volume_value.text(), '100')
        self.device.get_volume = Mock(return_value={'volume': 37, 'mute': False})
        self.window.amplifier_client._expire()
        self.wait(lambda: self.device.get_volume.called and not self.window.poll_running)
        self.assertEqual(self.window.volume_value.text(), '37')

    def test_calibration_keeps_physical_tv_audio_polling_when_amplifier_routed(self):
        self.settle_volume_controls()
        self.feed_amplifier_state()
        self.window.calibration_active = True
        self.device.get_volume = Mock(return_value={
            'volume': 28, 'mute': False, 'minVolume': 0, 'maxVolume': 100})
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running)
        self.device.get_volume.assert_called_once()
        self.assertEqual(self.window.last_volume['volume'], 28)
        self.assertGreater(self.window._calibration_snapshot()['valid_for_ms'], 0)
        self.window.calibration_active = False

    def test_routing_change_during_power_read_skips_audio_request(self):
        self.settle_volume_controls()
        jobs = []
        with patch.object(self.window, 'run_worker', side_effect=lambda *args: jobs.append(args)):
            self.window.poll_status()
        self.device.get_power = lambda: self.feed_amplifier_state() and 'active'
        self.device.get_volume = Mock()
        query, done, _, finished = jobs[0]
        done(query())
        finished()
        self.device.get_volume.assert_not_called()
        self.assertEqual(self.window.volume_value.text(), '100')

    def test_hidden_fallback_defers_refresh_until_window_is_shown(self):
        self.settle_volume_controls()
        self.feed_amplifier_state()
        self.window.hide()
        self.device.get_volume = Mock(return_value={'volume': 38, 'mute': False})
        self.window.amplifier_client._expire()
        self.assertTrue(self.window.polling.pending)
        self.assertFalse(self.window.poll_running)
        self.device.get_volume.assert_not_called()
        self.window.show()
        self.wait(lambda: self.device.get_volume.called and not self.window.poll_running)
        self.assertEqual(self.window.volume_value.text(), '38')

    def test_unrelated_amplifier_updates_keep_pending_tv_target(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        self.window.volume_value.setText('40')
        self.window.amplifier_client.state_changed.emit(None)
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.last_volume['volume'], 20)

    def test_status_poll_skips_volume_but_keeps_power_and_input_polling(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        self.window.volume_value.setText('40')
        self.device.get_volume = Mock(return_value={'volume': 25, 'mute': True})
        previous_power = self.device.calls['power']
        previous_inputs = self.device.calls['inputs']
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running and not self.window._input_status_running)
        self.device.get_volume.assert_not_called()
        self.assertGreater(self.device.calls['power'], previous_power)
        self.assertGreater(self.device.calls['inputs'], previous_inputs)
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.last_volume['volume'], 20)
        self.assertEqual(self.window.mute_button.text(), 'Mute')

    def test_old_poll_cannot_overwrite_a_completed_target(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        deliver = self.pending_poll_result()  # TV still reported 20.
        volume_at, sequence = self.window._volume_observed_at, self.window._status_sequence
        self.window._desired_volume = None
        self.window._volume_rate_loop_running = False
        self.window.last_volume = dict(self.window.last_volume, volume=40)
        self.window.update_volume_ui(self.window.last_volume)
        deliver()
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.last_volume['volume'], 40)
        self.assertEqual(self.window._volume_observed_at, volume_at)
        self.assertGreater(self.window._status_sequence, sequence)

    def test_poll_before_a_new_request_cannot_undo_its_completed_volume(self):
        self.settle_volume_controls()
        deliver = self.pending_poll_result()  # No target yet; TV reported 20.
        with patch.object(self.window, 'run_command', return_value=True):
            self.window.adjust_volume(1)
        # A fast request can complete before the older background poll arrives.
        self.window._volume_rate_loop_running = False
        self.window._desired_volume = None
        self.window.last_volume = dict(self.window.last_volume, volume=21)
        self.window.update_volume_ui(self.window.last_volume)
        deliver()
        self.assertEqual(self.window.volume_value.text(), '21')
        self.assertEqual(self.window.last_volume['volume'], 21)

    def test_limiter_owns_mute_feedback_and_does_not_wait_for_existing_poll(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        self.window.poll_running = True
        self.device.get_volume = Mock(return_value={'volume': 25, 'mute': True})
        self.device.adjust_volume = Mock()
        jobs = []
        power_time = self.window._status_observed_at
        with patch.object(self.window, 'run_command', side_effect=lambda function, **callbacks:
                          jobs.append((function, callbacks)) or True):
            self.window._volume_rate_step()
        self.assertEqual(len(jobs), 1)
        function, callbacks = jobs[0]
        result = function()
        callbacks['on_result'](result)
        self.window.poll_running = False
        self.device.get_volume.assert_called_once()
        self.device.adjust_volume.assert_called_once_with(1)
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.mute_button.text(), 'Unmute')
        self.assertEqual(self.window.last_volume['volume'], 25)
        self.assertEqual(self.window._status_observed_at, power_time)
        self.assertGreaterEqual(self.window._volume_observed_at, power_time)

    def test_limiter_start_during_power_read_skips_ordinary_audio_read(self):
        self.settle_volume_controls()
        def power():
            self.window._volume_rate_loop_running = True
            self.window._desired_volume = 40
            return 'active'
        self.device.get_power = power
        self.device.get_volume = Mock()
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running)
        self.device.get_volume.assert_not_called()
        self.assertEqual(self.window._desired_volume, 40)

    def test_inflight_ordinary_audio_cannot_overwrite_newer_limiter_feedback(self):
        self.settle_volume_controls()
        deliver = self.pending_poll_result()
        with patch.object(self.window, 'run_command', return_value=True):
            self.window.adjust_volume(20)
        info = dict(self.window.last_volume, volume=25, mute=True)
        self.window._accept_volume_status(info, time.monotonic())
        observed_at = self.window._volume_observed_at
        self.window.update_volume_ui(info)
        deliver()
        self.assertEqual(self.window.last_volume, info)
        self.assertEqual(self.window._volume_observed_at, observed_at)
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.mute_button.text(), 'Unmute')

    def test_volume_polling_resumes_after_limiter_stops(self):
        self.settle_volume_controls()
        self.window._volume_rate_loop_running = True
        self.device.get_volume = Mock(return_value={'volume': 30, 'mute': False})
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running)
        self.device.get_volume.assert_not_called()
        self.window._volume_rate_loop_running = False
        self.window._desired_volume = None
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running)
        self.device.get_volume.assert_called_once()
        self.assertEqual(self.window.last_volume['volume'], 30)

    def test_calibration_freshness_uses_oldest_power_or_volume_observation(self):
        self.settle_volume_controls()
        for power_at, volume_at, expected in ((90, 100, 0), (100, 90, 0), (99, 98, 4000)):
            self.window._status_observed_at = power_at
            self.window._volume_observed_at = volume_at
            with patch.dict(GLOBALS, time=Mock(monotonic=Mock(return_value=100))):
                state = self.window._calibration_snapshot()
            self.assertEqual(state['valid_for_ms'], expected)

    def test_unlimited_commands_keep_target_until_a_poll_started_after_completion(self):
        self.settle_volume_controls()
        self.window.config['VolumeRateLimit']['Active'] = False
        jobs = []
        def enqueue(function, **callbacks):
            jobs.append(callbacks)
            return True
        with patch.object(self.window, 'run_command', side_effect=enqueue):
            for _ in range(3):
                self.window.adjust_volume(1)
        self.assertEqual(self.window._pending_volume_commands, 3)
        self.window.amplifier_client.state_changed.emit(None)
        self.assertEqual(self.window.volume_value.text(), '23')
        deliver = self.pending_poll_result()
        for callbacks in jobs:
            callbacks['on_finished']()
        self.assertEqual(self.window._pending_volume_commands, 0)
        deliver()  # Began while commands were pending, so cannot erase target.
        self.window.amplifier_client.state_changed.emit(None)
        self.assertEqual(self.window.volume_value.text(), '23')
        self.window.poll_status()  # A fresh idle poll may now reconcile with TV.
        self.wait(lambda: not self.window.poll_running)
        self.assertIsNone(self.window._desired_volume)
        self.assertEqual(self.window.volume_value.text(), '20')

    def test_rejected_unlimited_command_does_not_leave_a_phantom_pending_target(self):
        self.settle_volume_controls()
        self.window.config['VolumeRateLimit']['Active'] = False
        with patch.object(self.window, 'run_command', return_value=False):
            self.window.adjust_volume(1)
        self.assertEqual(self.window._pending_volume_commands, 0)
        self.assertIsNone(self.window._desired_volume)
        self.assertEqual(self.window.volume_value.text(), '20')

    def check_button_hold(self, button, amount):
        self.settle_volume_controls()
        with patch.object(self.window, 'adjust_volume') as adjust:
            QTest.mousePress(button, Qt.LeftButton)
            adjust.assert_called_once_with(amount)
            QTest.qWait(320)
            self.assertEqual(adjust.call_count, 1)
            QTest.qWait(300)
            self.assertGreaterEqual(adjust.call_count, 2)
            count = adjust.call_count
            QTest.mouseRelease(button, Qt.LeftButton)
            QTest.qWait(160)
            self.assertEqual(adjust.call_count, count)
            self.assertTrue(all(call.args == (amount,) for call in adjust.call_args_list))

    def test_volume_up_repeats_after_half_second_and_stops_on_release(self):
        self.check_button_hold(self.window.volume_up, 1)

    def test_volume_down_repeats_after_half_second_and_stops_on_release(self):
        self.check_button_hold(self.window.volume_down, -1)

    def test_short_volume_click_changes_once(self):
        self.settle_volume_controls()
        with patch.object(self.window, 'adjust_volume') as adjust:
            QTest.mouseClick(self.window.volume_up, Qt.LeftButton)
            adjust.assert_called_once_with(1)

    def test_disabling_a_held_button_stops_repeats(self):
        self.settle_volume_controls()
        with patch.object(self.window, 'adjust_volume') as adjust:
            QTest.mousePress(self.window.volume_up, Qt.LeftButton)
            QTest.qWait(550)
            count = adjust.call_count
            self.window.volume_up.setEnabled(False)
            QTest.qWait(200)
            QTest.mouseRelease(self.window.volume_up, Qt.LeftButton)
            self.assertEqual(adjust.call_count, count)

    def test_release_keeps_limited_target_until_tv_catches_up(self):
        self.settle_volume_controls()
        self.device.adjust_volume = Mock()
        QTest.mousePress(self.window.volume_up, Qt.LeftButton)
        QTest.qWait(620)
        QTest.mouseRelease(self.window.volume_up, Qt.LeftButton)
        target = self.window._desired_volume
        self.assertGreaterEqual(target, 22)
        QTest.qWait(160)
        self.assertEqual(self.window._desired_volume, target)
        self.assertTrue(self.window._volume_rate_loop_running)
        self.device.get_volume = Mock(return_value=dict(self.window.last_volume, volume=target))
        self.wait(lambda: not self.window._volume_rate_loop_running)
        self.assertEqual(self.window.volume_value.text(), str(target))


    def test_volume_event_refresh_skips_only_recent_audio_on_periodic_tick(self):
        self.settle_volume_controls()
        before = self.device.calls.copy()
        self.window._request_poll(groups={'volume'})
        self.wait(lambda: not self.window.poll_running)
        self.assertEqual(self.device.calls['volume'], before['volume'] + 1)
        self.assertEqual(self.device.calls['power'], before['power'])
        self.assertEqual(self.device.calls['inputs'], before['inputs'])
        self.window.polling.invalidate({'power', 'inputs'})
        self.window.polling._periodic_due()
        self.wait(lambda: not self.window.poll_running and not self.window._input_status_running)
        self.assertEqual(self.device.calls['power'], before['power'] + 1)
        self.assertEqual(self.device.calls['inputs'], before['inputs'] + 1)
        self.assertEqual(self.device.calls['volume'], before['volume'] + 1)

    def test_scheduled_audio_refresh_does_not_defer_power_or_inputs(self):
        self.settle_volume_controls()
        before = self.device.calls.copy()
        self.window.polling.invalidate()
        self.window._request_poll(1000, groups={'volume'})
        self.window.polling._periodic_due()
        self.wait(lambda: not self.window.poll_running and not self.window._input_status_running)
        self.assertEqual(self.device.calls['power'], before['power'] + 1)
        self.assertEqual(self.device.calls['inputs'], before['inputs'] + 1)
        self.assertEqual(self.device.calls['volume'], before['volume'])
        self.assertTrue(self.window.polling.pending)

    def test_input_event_refresh_does_not_read_power_or_volume(self):
        self.settle_volume_controls()
        before = self.device.calls.copy()
        self.window._request_poll(groups={'inputs'})
        self.wait(lambda: not self.window._input_status_running)
        self.assertEqual(self.device.calls['inputs'], before['inputs'] + 1)
        self.assertEqual(self.device.calls['power'], before['power'])
        self.assertEqual(self.device.calls['volume'], before['volume'])
        self.assertNotIn('inputs', self.window.polling.periodic_groups())

    def test_failed_event_refresh_leaves_periodic_retry_available(self):
        self.settle_volume_controls()
        self.device.failures['volume'] = REMOTE['SonyConnectionError']('TV unavailable')
        self.window._request_poll(groups={'volume'})
        self.wait(lambda: not self.window.poll_running)
        self.assertFalse(self.window.polling.pending)
        self.assertNotIn('volume', self.window.polling._observed)
        self.device.failures.clear()
        before = self.device.calls['volume']
        self.window.polling._periodic_due()
        self.wait(lambda: not self.window.poll_running)
        self.assertEqual(self.device.calls['volume'], before + 1)
        self.assertEqual(self.window.last_volume['volume'], 20)

    def test_queued_mute_command_reserves_audio_until_followup(self):
        self.settle_volume_controls()
        release = threading.Event()
        started = threading.Event()
        def set_mute(value):
            started.set()
            release.wait(2)
        self.device.set_mute = set_mute
        self.addCleanup(release.set)
        before = self.device.calls.copy()
        self.window.toggle_mute()
        self.assertEqual(self.window.polling._commands['volume'], 1)
        self.wait(started.is_set)
        self.window.polling.invalidate({'power', 'inputs'})
        self.window.polling._periodic_due()
        self.wait(lambda: not self.window.poll_running and not self.window._input_status_running)
        self.assertEqual(self.device.calls['volume'], before['volume'])
        self.assertEqual(self.device.calls['power'], before['power'] + 1)
        self.assertEqual(self.device.calls['inputs'], before['inputs'] + 1)
        release.set()
        self.wait(lambda: self.device.calls['volume'] == before['volume'] + 1
                  and not self.window.poll_running)
        self.assertFalse(self.window.polling._commands['volume'])

if __name__ == '__main__':
    unittest.main()
