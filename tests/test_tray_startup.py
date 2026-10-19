"""Startup visibility checks without real tray icons, devices, or instance locks."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Sony' if (ROOT / 'SonyRemote.pyw').exists() else 'Yamaha'
SCRIPT = ROOT / f'{NAME}Remote.pyw'
REMOTE = runpy.run_path(str(SCRIPT), run_name='tray_startup_tests')
GLOBALS = REMOTE['_run_application'].__globals__


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.tray = Mock()
        self.shows = 0
        self._shutdown = Mock()
        self._update_polling_state = Mock()
        self._fit_and_lock_window = Mock()

    def showEvent(self, event):
        self.shows += 1
        super().showEvent(event)


class TrayStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.config = REMOTE['_default_config_copy']()
        self.config.update(Host='device.local', SystemTray=True, CloseToTray=True)
        if NAME == 'Sony':
            self.config['PreSharedKey'] = 'test-only'
        self.window = Window()
        self.addCleanup(self.cleanup)
        self.application = Mock()
        self.application.exec.return_value = 0
        self.constructor = Mock(return_value=self.window)
        self.config_loader = Mock(return_value=self.config)
        self.missing_settings = Mock()
        self.acquire = Mock(return_value=True)
        self.activate = Mock(return_value=True)
        replacements = {
            'QApplication': Mock(return_value=self.application),
            'TooltipDelayStyle': Mock(), 'get_app_icon': Mock(),
            'set_app_user_model_id': Mock(), 'configure_diagnostics': Mock(),
            'acquire_single_instance': self.acquire,
            'activate_existing_instance': self.activate,
            'load_config': self.config_loader,
            'show_missing_config_settings': self.missing_settings,
            f'{NAME}Remote': self.constructor,
        }
        context = patch.dict(GLOBALS, replacements)
        context.start()
        self.addCleanup(context.stop)
        tray = patch.object(REMOTE['QSystemTrayIcon'], 'isSystemTrayAvailable', return_value=True)
        self.available = tray.start()
        self.addCleanup(tray.stop)

    def cleanup(self):
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def run_startup(self, *arguments):
        with patch.object(sys, 'argv', [str(SCRIPT), *arguments]):
            self.assertEqual(REMOTE['_run_application'](), 0)

    def assert_visible(self):
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.shows, 1)

    def test_flag_with_tray_settings_starts_hidden_without_a_show_event(self):
        self.run_startup('--start-in-tray')
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.window.shows, 0)
        self.window._update_polling_state.assert_called_once_with()
        self.application.setQuitOnLastWindowClosed.assert_called_once_with(False)
        self.missing_settings.assert_called_once_with(self.config)
        self.window._shutdown.assert_called_once()

    def test_without_flag_starts_visible_even_when_tray_is_enabled(self):
        self.run_startup()
        self.assert_visible()

    def test_close_to_tray_disabled_starts_visible(self):
        self.config['CloseToTray'] = False
        self.run_startup('--start-in-tray')
        self.assert_visible()

    def test_system_tray_disabled_starts_visible(self):
        self.config.update(SystemTray=False, CloseToTray=False)
        self.window.tray = None
        self.run_startup('--start-in-tray')
        self.assert_visible()
        self.application.setQuitOnLastWindowClosed.assert_called_once_with(True)

    def test_unavailable_desktop_tray_starts_visible(self):
        self.available.return_value = False
        self.run_startup('--start-in-tray')
        self.assert_visible()

    def test_no_created_tray_icon_starts_visible(self):
        self.window.tray = None
        self.run_startup('--start-in-tray')
        self.assert_visible()
        self.application.setQuitOnLastWindowClosed.assert_called_once_with(True)

    def test_missing_settings_feedback_is_still_requested_when_starting_hidden(self):
        self.config['Host'] = ''
        self.run_startup('--start-in-tray')
        self.missing_settings.assert_called_once_with(self.config)
        self.assertEqual(self.window.shows, 0)

    def test_existing_instance_is_activated_regardless_of_start_in_tray(self):
        self.acquire.return_value = False
        self.run_startup('--start-in-tray')
        self.activate.assert_called_once_with('tv' if NAME == 'Sony' else 'amplifier')
        self.config_loader.assert_not_called()
        self.constructor.assert_not_called()
        self.application.exec.assert_not_called()

    def test_configuration_restart_preserves_start_in_tray_argument(self):
        with patch.object(sys, 'argv', [str(SCRIPT), '--start-in-tray']), \
             patch.object(REMOTE['QProcess'], 'startDetached', return_value=(True, 123)) as launch:
            REMOTE['launch_replacement']()
        self.assertEqual(launch.call_args.args[1], [str(SCRIPT.resolve()), '--start-in-tray'])


if __name__ == '__main__':
    unittest.main()
