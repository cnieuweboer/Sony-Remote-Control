"""Warning decisions stay on Qt's GUI thread; worker heartbeats keep running."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import runpy
import time
import unittest
from unittest.mock import Mock, patch
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
HELPER = runpy.run_path(str(ROOT / 'VolumeCalibration.pyw'), run_name='isolation_dialog_tests')


class DialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def test_dialog_defaults_to_cancel_and_returns_explicit_continue(self):
        window = HELPER['CalibrationWindow']()
        answers = []
        window.worker = type('Worker', (), {'answer_isolation': lambda self, value: answers.append(value)})()
        box_cls = HELPER['QMessageBox']
        def execute(box):
            self.assertEqual(box.defaultButton().text(), 'Cancel')
            self.assertIn('entire sweep', box.text())
            self.assertIn('five minutes', box.text())
            proceed = next(button for button in box.buttons() if button.text().startswith('Continue'))
            proceed.click()
            return 0
        with patch.object(box_cls, 'exec', execute):
            window.show_isolation_warning('Could not verify OFF.')
        self.assertEqual(answers, [True])
        self.assertIsNone(window._isolation_box)
        window.worker = None
        window.close()
        window.deleteLater()

    def test_timeout_uses_real_deadline_logs_and_keeps_pumping(self):
        worker = HELPER['SessionWorker']()
        worker._isolation_answer = Mock()
        worker._isolation_answer.wait.return_value = False
        logs = []
        worker.progress.connect(logs.append)
        pump = Mock()
        clock = Mock()
        clock.monotonic.side_effect = [0, 299, 300]
        self.assertEqual(HELPER['ISOLATION_DECISION_SECONDS'], 300)
        with patch.dict(HELPER['SessionWorker'].confirm_isolation.__globals__, time=clock):
            with self.assertRaisesRegex(HELPER['Cancelled'], 'timed out'):
                worker.confirm_isolation('Isolation unavailable.', pump)
        pump.assert_called_once()
        self.assertTrue(any('timed out' in line for line in logs))
        worker.deleteLater()

    def test_timeout_closes_dialog_on_restoration_or_completion(self):
        for during_cleanup in (False, True):
            with self.subTest(during_cleanup=during_cleanup):
                class Session:
                    def __init__(self, *args, **kwargs):
                        self.confirm = kwargs['confirm_isolation']
                        self.cleanup = kwargs['cleanup_started']
                    def isolation_summary(self):
                        return ''
                    def run(self):
                        if during_cleanup:
                            self.cleanup()
                        try:
                            self.confirm('Speaker state unavailable.', lambda: None)
                        except HELPER['Cancelled']:
                            if not during_cleanup:
                                self.cleanup()
                            return 'cleanup incomplete' if during_cleanup else 'cancelled and restored'
                window = HELPER['CalibrationWindow']()
                worker = HELPER['SessionWorker']()
                window.worker = worker
                results, cleanups = [], []
                worker.isolation_warning.connect(window.show_isolation_warning)
                worker.cleanup_started.connect(window.show_restoring)
                worker.cleanup_started.connect(lambda: cleanups.append(True))
                worker.completed.connect(window.completed)
                worker.completed.connect(results.append)
                with patch.dict(HELPER['SessionWorker'].run.__globals__,
                                CalibrationSession=Session, ISOLATION_DECISION_SECONDS=.12), \
                        patch.object(HELPER['QMessageBox'], 'warning'):
                    worker.start()
                    deadline = time.monotonic() + 3
                    while worker.isRunning() and time.monotonic() < deadline:
                        self.app.processEvents()
                        time.sleep(.005)
                    self.app.processEvents()
                    self.assertFalse(worker.isRunning())
                self.assertEqual(len(cleanups), 1)
                self.assertIsNone(window._isolation_box)
                self.assertEqual(results, ['cleanup incomplete' if during_cleanup else 'cancelled and restored'])
                window.worker = None
                window.close()
                window.deleteLater()
                worker.deleteLater()

    def test_worker_pumps_while_waiting_for_gui_decision(self):
        pumps = []
        class Session:
            def __init__(self, *args, **kwargs):
                self.confirm = kwargs['confirm_isolation']
                self.cleanup = kwargs['cleanup_started']
            def isolation_summary(self):
                return ''
            def run(self):
                accepted = self.confirm('Isolation lost.', lambda: pumps.append(time.monotonic()))
                self.cleanup()
                return 'continued' if accepted else 'cancelled'
        worker = HELPER['SessionWorker']()
        results = []
        worker.isolation_warning.connect(lambda reason: QTimer.singleShot(180, lambda: worker.answer_isolation(True)))
        worker.completed.connect(results.append)
        with patch.dict(HELPER['SessionWorker'].run.__globals__, CalibrationSession=Session):
            worker.start()
            deadline = time.monotonic() + 3
            while worker.isRunning() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.app.processEvents()
            self.assertFalse(worker.isRunning())
        self.assertEqual(results, ['continued'])
        self.assertGreaterEqual(len(pumps), 2)
        worker.deleteLater()


if __name__ == '__main__':
    unittest.main()
