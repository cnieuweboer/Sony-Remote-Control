"""Force hook startup/stop interleavings with a fake Win32 API and real threads."""
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
IS_TV = ROOT.name == 'Sony-Remote-Control'
REMOTE = runpy.run_path(str(ROOT / ('SonyRemote.pyw' if IS_TV else 'YamahaRemote.pyw')), run_name='hook_lifecycle_tests')
HOOK = REMOTE['_WindowsShortcutHook' if IS_TV else '_WindowsVolumeHook']
LOADER = '_load_shortcut_hook_api' if IS_TV else '_load_volume_hook_api'


class HookLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.quit = threading.Event()
        self.api = SimpleNamespace(
            PeekMessageW=Mock(return_value=0), GetAsyncKeyState=Mock(return_value=0),
            SetWindowsHookExW=Mock(return_value=123), UnhookWindowsHookEx=Mock(return_value=1),
            GetMessageW=Mock(side_effect=self.get_message),
            PostThreadMessageW=Mock(side_effect=self.post_quit),
        )
        self.kernel = SimpleNamespace(GetCurrentThreadId=Mock(return_value=456), GetModuleHandleW=Mock(return_value=789))
        self.hook = HOOK(lambda *_: True)
        self.hook._notify = Mock()
        self.diagnostics = Mock()
        self.patches = patch.dict(HOOK._run.__globals__, {
            LOADER: lambda: (self.api, self.kernel), 'record_exception': self.diagnostics,
        })
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.addCleanup(self.cleanup_thread)

    def cleanup_thread(self):
        self.release.set()
        self.quit.set()
        self.hook.stop()

    def get_message(self, *_):
        self.entered.set()
        if not self.quit.wait(2):
            raise RuntimeError('Fake message loop did not receive shutdown')
        return 0

    def post_quit(self, thread_id, message, *_):
        self.assertEqual((thread_id, message), (456, 0x12))
        self.quit.set()
        return 1

    def block_startup(self):
        self.entered.set()
        if not self.release.wait(2):
            raise RuntimeError('Test did not release startup')

    def stop_during_startup(self):
        self.hook.start()
        self.assertTrue(self.entered.wait(1))
        # stop() sets its flag before joining; release startup only after that.
        stopper = threading.Thread(target=self.hook.stop, daemon=True)
        stopper.start()
        try:
            self.assertTrue(self.hook._stopping.wait(1))
        finally:
            self.release.set()
            stopper.join(3)
        self.assertFalse(stopper.is_alive())
        self.assert_stopped()

    def assert_stopped(self):
        self.assertFalse(self.hook._thread.is_alive())
        self.assertFalse(self.hook.installed)
        self.assertIsNone(self.hook._hook)
        self.assertIsNone(self.hook._callback)
        self.assertIsNone(self.hook._thread_id)
        self.diagnostics.assert_not_called()
        self.assertFalse(self.hook._faulted)

    def test_stop_before_start_skips_installation(self):
        self.hook.stop()
        self.hook.start()
        self.hook._thread.join(1)
        self.assert_stopped()
        self.api.SetWindowsHookExW.assert_not_called()
        self.hook._notify.assert_called_once_with(False, None)

    def test_stop_before_thread_id_exists_skips_installation(self):
        def loader():
            self.block_startup()
            return self.api, self.kernel
        with patch.dict(HOOK._run.__globals__, {LOADER: loader}):
            self.stop_during_startup()
        self.api.SetWindowsHookExW.assert_not_called()
        self.api.PostThreadMessageW.assert_not_called()
        self.hook._notify.assert_called_once_with(False, None)

    def test_stop_during_installation_unhooks_without_started_notification(self):
        def install(*_):
            self.block_startup()
            return 123
        self.api.SetWindowsHookExW.side_effect = install
        self.stop_during_startup()
        self.api.UnhookWindowsHookEx.assert_called_once_with(123)
        self.api.GetMessageW.assert_not_called()
        self.hook._notify.assert_called_once_with(False, None)

    def test_running_loop_is_woken_and_hook_released(self):
        self.hook.start()
        self.assertTrue(self.entered.wait(1))
        self.assertTrue(self.hook.installed)
        self.hook.stop()
        self.assert_stopped()
        self.api.PostThreadMessageW.assert_called_once()
        self.api.UnhookWindowsHookEx.assert_called_once_with(123)
        self.assertEqual(self.hook._notify.call_args_list, [((True,), {}), ((False, None), {})])

    def test_repeated_stop_does_not_unhook_twice(self):
        self.hook.start()
        self.assertTrue(self.entered.wait(1))
        self.hook.stop()
        self.hook.stop()
        self.assert_stopped()
        self.api.UnhookWindowsHookEx.assert_called_once_with(123)
        self.api.PostThreadMessageW.assert_called_once()


if __name__ == '__main__':
    unittest.main()
