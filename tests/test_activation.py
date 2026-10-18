"""Real local-socket and second-process activation checks; no device backend."""
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QLockFile, QTimer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtWidgets import QApplication, QDialog, QMainWindow

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'SonyRemote.pyw'
REMOTE = runpy.run_path(str(SCRIPT), run_name='activation_tests')
GLOBALS = REMOTE['ActivationServer'].__init__.__globals__
ROLE = 'tv' if 'SonyRemote' in REMOTE else 'amplifier'
CLS = REMOTE['SonyRemote' if ROLE == 'tv' else 'YamahaRemote']

CHILD = r'''
import runpy, sys
from PySide6.QtCore import QLockFile
m = runpy.run_path(sys.argv[1], run_name='second_launch')
g = m['_run_application'].__globals__
g['IPC_USER_SUFFIX'] = sys.argv[2]
def acquire():
    lock = QLockFile(sys.argv[3])
    lock.setStaleLockTime(0)
    g['_test_instance_lock'] = lock
    return lock.tryLock(0)
def forbidden_config_read():
    raise AssertionError('Second launch must not load device configuration')
g['acquire_single_instance'] = acquire
g['load_config'] = forbidden_config_read
g['QMessageBox'].warning = lambda *args: sys.stderr.write('ACTIVATION_UNAVAILABLE\n')
g['QMessageBox'].critical = lambda *args: sys.stderr.write('UNEXPECTED_STARTUP_ERROR\n')
sys.exit(m['_run_application']())
'''


class Window(QMainWindow):
    show_from_tray = CLS.show_from_tray
    _activate_existing_window = CLS._activate_existing_window

    def __init__(self):
        super().__init__()
        self._shutting_down = False
        self.refreshes = 0
        self.resize(240, 120)

    def _update_polling_state(self, refresh=False):
        self.refreshes += int(refresh)


class ActivationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.suffix = 'test-' + uuid.uuid4().hex[:16]
        self.context = patch.dict(GLOBALS, {'IPC_USER_SUFFIX': self.suffix})
        self.context.start()
        self.addCleanup(self.context.stop)
        self.lock_path = Path(self.folder.name) / 'instance.lock'
        self.instance_lock = QLockFile(str(self.lock_path))
        self.instance_lock.setStaleLockTime(0)
        self.assertTrue(self.instance_lock.tryLock(0))
        self.window = Window()
        self.children = []
        self.sockets = []
        self.server = REMOTE['ActivationServer'](ROLE, self.window._activate_existing_window, self.window)
        self.assertTrue(self.server.available)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.communicate()
        for socket in self.sockets:
            socket.abort()
        self.server.close()
        self.instance_lock.unlock()
        self.window.hide()
        self.window.deleteLater()
        self.app.processEvents()

    def wait(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.002)
        self.fail('Activation check timed out')

    def child(self):
        child = subprocess.Popen([sys.executable, '-c', CHILD, str(SCRIPT), self.suffix, str(self.lock_path)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'})
        self.children.append(child)
        return child

    def launch(self, expected=0):
        child = self.child()
        self.wait(lambda: child.poll() is not None)
        stdout, stderr = child.communicate()
        self.assertEqual(child.returncode, expected, stdout + stderr)
        if expected:
            self.assertIn('ACTIVATION_UNAVAILABLE', stderr)
        return child

    def connect(self):
        socket = QLocalSocket()
        self.sockets.append(socket)
        socket.connectToServer(self.server.name)
        self.assertTrue(socket.waitForConnected(250))
        self.wait(lambda: bool(self.server.clients))
        return socket

    def test_second_process_restores_hidden_window_without_loading_config(self):
        self.window.hide()
        self.launch()
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.refreshes, 1)

    def test_second_process_restores_minimized_window(self):
        self.window.showMinimized()
        self.app.processEvents()
        self.assertTrue(self.window.isMinimized())
        self.launch()
        self.assertFalse(self.window.isMinimized())
        self.assertTrue(self.window.isVisible())

    def test_activation_preserves_calibration_disabled_controls(self):
        self.window.setEnabled(False)
        self.launch()
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.isEnabled())

    def test_existing_modal_dialog_remains_visible_and_modal(self):
        dialog = QDialog(self.window)
        dialog.setModal(True)
        dialog.show()
        self.app.processEvents()
        self.launch()
        self.assertTrue(dialog.isVisible())
        self.assertTrue(dialog.isModal())
        self.assertIs(QApplication.activeModalWidget(), dialog)
        dialog.close()

    def test_two_second_launches_share_existing_window(self):
        first, second = self.child(), self.child()
        self.wait(lambda: first.poll() is not None and second.poll() is not None)
        for child in (first, second):
            stdout, stderr = child.communicate()
            self.assertEqual(child.returncode, 0, stdout + stderr)
        self.assertEqual(self.window.refreshes, 2)
        self.assertTrue(self.window.isVisible())

    def test_unavailable_endpoint_reports_failure_instead_of_silent_exit(self):
        self.server.close()
        started = time.monotonic()
        self.launch(expected=1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(self.window.isVisible())

    def test_second_launch_retries_while_first_endpoint_is_starting(self):
        self.server.close()
        def ready():
            self.window.activation_server = self.server
            CLS._setup_activation(self.window)
            self.server = self.window.activation_server
        QTimer.singleShot(500, ready)
        self.launch()
        self.assertTrue(self.server.available)
        self.assertTrue(self.window.isVisible())

    def test_wrong_role_in_handshake_is_rejected(self):
        self.server.role = 'amplifier' if ROLE == 'tv' else 'tv'
        self.launch(expected=1)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.window.refreshes, 0)

    def test_shutting_down_window_rejects_activation(self):
        self.window._shutting_down = True
        self.launch(expected=1)
        self.assertFalse(self.window.isVisible())

    def test_malformed_or_device_command_request_cannot_activate(self):
        socket = self.connect()
        socket.write(REMOTE['_ipc_encode']({'version': 1, 'type': 'command', 'command': 'volume_up'}))
        socket.flush()
        self.wait(lambda: socket.state() == QLocalSocket.UnconnectedState)
        self.assertFalse(self.window.isVisible())
        self.assertEqual(self.window.refreshes, 0)

    def test_partial_activation_request_waits_for_complete_frame(self):
        socket = self.connect()
        payload = REMOTE['_ipc_encode']({'version': 1, 'type': 'activate'})
        socket.write(payload[:7])
        socket.flush()
        self.wait(lambda: any(self.server.clients.values()))
        self.assertFalse(self.window.isVisible())
        socket.write(payload[7:])
        socket.flush()
        self.wait(self.window.isVisible)
        self.assertEqual(self.window.refreshes, 1)

    def test_idle_client_is_disconnected_without_activation(self):
        socket = self.connect()
        self.wait(lambda: socket.state() == QLocalSocket.UnconnectedState, timeout=2)
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.server.clients)

    def test_duplicate_server_cannot_replace_active_endpoint(self):
        duplicate = REMOTE['ActivationServer'](ROLE, lambda: False)
        try:
            self.assertFalse(duplicate.available)
            self.launch()
            self.assertTrue(self.window.isVisible())
        finally:
            duplicate.close()

    def test_endpoint_recreated_after_pause_can_activate_again(self):
        self.server.close()
        self.window.activation_server = self.server
        CLS._setup_activation(self.window)
        self.server = self.window.activation_server
        self.assertTrue(self.server.available)
        self.launch()
        self.assertTrue(self.window.isVisible())

    def test_endpoint_name_is_user_local_and_role_based(self):
        self.assertEqual(self.server.name, f'remote-control.{ROLE}.activation.v1.{self.suffix}')
        self.assertTrue(self.server.server.socketOptions() & REMOTE['QLocalServer'].UserAccessOption)


class ForegroundPermissionTests(unittest.TestCase):
    def test_windows_permission_is_granted_to_existing_process(self):
        api = SimpleNamespace(AllowSetForegroundWindow=Mock(return_value=True))
        with patch.dict(GLOBALS, {'sys': SimpleNamespace(platform='win32')}), \
             patch.object(REMOTE['ctypes'], 'WinDLL', return_value=api, create=True) as load:
            REMOTE['_allow_foreground_activation'](1234)
        load.assert_called_once_with('user32', use_last_error=True)
        api.AllowSetForegroundWindow.assert_called_once_with(1234)
        self.assertEqual(api.AllowSetForegroundWindow.argtypes, [REMOTE['ctypes'].c_uint32])

    def test_missing_foreground_api_does_not_prevent_restoration_request(self):
        with patch.dict(GLOBALS, {'sys': SimpleNamespace(platform='win32')}), \
             patch.object(REMOTE['ctypes'], 'WinDLL', side_effect=OSError('Unavailable'), create=True):
            REMOTE['_allow_foreground_activation'](1234)


if __name__ == '__main__':
    unittest.main()
