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

    def test_unrelated_amplifier_updates_keep_pending_tv_target(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        self.window.volume_value.setText('40')
        self.window.amplifier_client.state_changed.emit(None)
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.last_volume['volume'], 20)

    def test_status_poll_keeps_target_and_updates_actual_volume_and_mute(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        self.window.volume_value.setText('40')
        self.device.get_volume = Mock(return_value={'volume': 25, 'mute': True})
        self.window.poll_status()
        self.wait(lambda: not self.window.poll_running)
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.last_volume['volume'], 25)
        self.assertEqual(self.window.mute_button.text(), 'Unmute')

    def test_old_poll_cannot_overwrite_a_completed_target(self):
        self.settle_volume_controls()
        self.window._desired_volume = 40
        self.window._volume_rate_loop_running = True
        deliver = self.pending_poll_result()  # TV still reported 20.
        observed_at, sequence = self.window._status_observed_at, self.window._status_sequence
        self.window._desired_volume = None
        self.window._volume_rate_loop_running = False
        self.window.last_volume = dict(self.window.last_volume, volume=40)
        self.window.update_volume_ui(self.window.last_volume)
        deliver()
        self.assertEqual(self.window.volume_value.text(), '40')
        self.assertEqual(self.window.last_volume['volume'], 40)
        self.assertEqual(self.window._status_observed_at, observed_at)
        self.assertEqual(self.window._status_sequence, sequence)

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


if __name__ == '__main__':
    unittest.main()
