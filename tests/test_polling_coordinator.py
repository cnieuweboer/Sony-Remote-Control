"""Scheduling policy with real Qt timers and no device or IPC connection."""
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock

from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

REMOTE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'SonyRemote.pyw'),
                        run_name='polling_coordinator_tests')


class PollingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = QWidget()
        self.w._shutting_down = self.w._calibration_operation = False
        self.w.calibration_active = self.w.poll_running = False
        self.w._volume_rate_loop_running = self.w._volume_rate_step_running = False
        self.w._amplifier_routed = False
        self.w._smtc_background_polling = Mock(return_value=False)
        self.w.config = dict(REMOTE['DEFAULT_CONFIG'], Host='192.168.1.2', PreSharedKey='test')
        self.w.poll_status = Mock()
        self.p = REMOTE['PollingCoordinator'](self.w)
        self.w.show()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p.pause()
        self.w.close()

    def test_overlapping_refreshes_coalesce(self):
        self.p.request(20)
        self.p.request(40)
        QTest.qWait(80)
        self.w.poll_status.assert_called_once_with()
        self.assertFalse(self.p.pending)

    def test_active_poll_defers_refresh_until_finished(self):
        self.w.poll_running = True
        self.p.request()
        QTest.qWait(10)
        self.w.poll_status.assert_not_called()
        self.w.poll_running = False
        self.p.finished()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with()

    def test_hidden_refresh_waits_for_visibility(self):
        self.w.hide()
        self.p.request()
        self.assertTrue(self.p.pending)
        self.w.poll_status.assert_not_called()
        self.w.show()
        self.p.update()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with()

    def test_pause_cancels_delayed_callbacks_and_resume_refreshes(self):
        self.p.request(20)
        self.p.pause()
        QTest.qWait(40)
        self.w.poll_status.assert_not_called()
        self.p.resume()
        self.w.poll_status.assert_called_once_with()
        self.assertTrue(self.p.periodic.isActive())

    def test_latest_conditional_completion_replaces_obsolete_one(self):
        self.p.request(10, lambda: False)
        self.p.request(20, lambda: True)
        QTest.qWait(50)
        self.w.poll_status.assert_called_once_with()

    def test_unconditional_refresh_is_not_cancelled_by_stale_volume_target(self):
        self.w.poll_running = True
        self.p.request()
        self.p.request(10, lambda: False)
        self.w.poll_running = False
        self.p.finished()
        QTest.qWait(20)
        self.w.poll_status.assert_called_once_with()

    def test_periodic_tick_does_not_preempt_command_settling(self):
        self.p.request(100)
        self.p._periodic_due()
        self.w.poll_status.assert_not_called()
        self.p.pause()

    def test_calibration_and_background_mode_allow_hidden_polling(self):
        self.w.hide()
        self.assertFalse(self.p.allowed())
        self.w.calibration_active = True
        self.p.update()
        self.assertTrue(self.p.allowed())
        self.assertEqual(self.p.periodic.interval(), 250)
        self.w._calibration_operation = True
        self.assertFalse(self.p.allowed())
        self.w._calibration_operation = self.w.calibration_active = False
        self.w._smtc_background_polling.return_value = True
        self.p.update()
        self.assertTrue(self.p.allowed())
        self.assertEqual(self.p.periodic.interval(), REMOTE['POLL_INTERVAL_MS'])

    def test_volume_ownership_policy(self):
        self.assertTrue(self.p.reads_volume())
        self.w._volume_rate_loop_running = True
        self.assertFalse(self.p.reads_volume())
        self.w._volume_rate_loop_running = False
        self.w._amplifier_routed = True
        self.assertFalse(self.p.reads_volume())
        self.w.calibration_active = True
        self.assertTrue(self.p.reads_volume())

    def test_missing_config_and_shutdown_block_requests(self):
        self.w.config['PreSharedKey'] = ''
        self.p.request()
        self.w.poll_status.assert_not_called()
        self.w.config['PreSharedKey'] = 'test'
        self.w._shutting_down = True
        self.p.update(refresh=True)
        self.assertFalse(self.p.periodic.isActive())
        self.w.poll_status.assert_not_called()

    def test_input_reads_require_discovery_and_no_calibration(self):
        self.w._input_status_running = False
        self.w._discovery_ready = {'inputs'}
        self.w._discovery_pending = set()
        self.assertTrue(self.p.reads_inputs())
        self.w._discovery_pending.add('inputs')
        self.assertFalse(self.p.reads_inputs())
        self.w._discovery_pending.clear()
        self.w.calibration_active = True
        self.assertFalse(self.p.reads_inputs())

    def test_periodic_tick_does_not_queue_behind_running_poll(self):
        self.w.poll_running = True
        self.p._periodic_due()
        self.assertFalse(self.p.pending)
        self.w.poll_running = False
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with()
