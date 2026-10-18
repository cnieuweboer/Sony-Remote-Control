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
             patch.object(REMOTE['SonyRemote'], '_install_windows_keyboard_hook', lambda window: None):
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

    def test_remote_codes_recovery_restores_navigation_and_shortcut_capabilities(self):
        self.device.failures['remote_codes'] = REMOTE['DeviceResponseError']('Unavailable')
        self.start_and_settle()
        self.assertFalse(self.buttons('Up')[0].isEnabled())
        self.device.failures.clear()
        self.window.initialize()
        self.wait(lambda: 'remote_codes' in self.window._discovery_ready)
        self.assertTrue(self.buttons('Up')[0].isEnabled())
        self.assertTrue(self.buttons('Settings')[0].isEnabled())
        self.assertIn('Up', self.window._shortcut_state[2])

    def test_input_recovery_restores_dropdown_and_favorites(self):
        self.device.failures['inputs'] = REMOTE['DeviceResponseError']('Unavailable')
        self.start_and_settle()
        self.device.failures.clear()
        self.window.initialize()
        self.wait(lambda: 'inputs' in self.window._discovery_ready)
        self.assertTrue(self.window.input_combo.isEnabled())
        self.assertTrue(self.window.input_favorites_layout.itemAt(0).widget().isEnabled())
        self.assertEqual(self.window.input_combo.currentData(), 'input')

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
             patch.object(REMOTE['SonyRemote'], '_setup_calibration', lambda window: None):
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


if __name__ == '__main__':
    unittest.main()
