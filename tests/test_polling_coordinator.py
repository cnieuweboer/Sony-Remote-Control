"""Scheduling policy with real Qt timers and no device or IPC connection."""
from pathlib import Path
import runpy
import unittest
from unittest.mock import Mock, patch

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
        self.w._pending_volume_commands = 0
        self.w._input_status_running = False
        self.w._discovery_ready = {'inputs'}
        self.w._discovery_pending = set()
        self.w._smtc_background_polling = Mock(return_value=False)
        self.w.config = dict(REMOTE['DEFAULT_CONFIG'], Host='192.168.1.2', PreSharedKey='test')
        self.w.poll_status = Mock()
        self.p = REMOTE['PollingCoordinator'](self.w)
        self.w.polling = self.p
        self.w.show()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p.pause()
        self.w.close()

    def test_overlapping_refreshes_coalesce(self):
        self.p.request(20)
        self.p.request(40)
        QTest.qWait(80)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)
        self.assertFalse(self.p.pending)

    def test_active_poll_defers_refresh_until_finished(self):
        self.w.poll_running = True
        self.p.request()
        QTest.qWait(10)
        self.w.poll_status.assert_not_called()
        self.w.poll_running = False
        self.p.finished()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_hidden_refresh_waits_for_visibility(self):
        self.w.hide()
        self.p.request()
        self.assertTrue(self.p.pending)
        self.w.poll_status.assert_not_called()
        self.w.show()
        self.p.update()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_pause_cancels_delayed_callbacks_and_resume_refreshes(self):
        self.p.request(20)
        self.p.pause()
        QTest.qWait(40)
        self.w.poll_status.assert_not_called()
        self.p.resume()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)
        self.assertTrue(self.p.periodic.isActive())

    def test_latest_conditional_completion_replaces_obsolete_one(self):
        self.p.request(10, lambda: False)
        self.p.request(20, lambda: True)
        QTest.qWait(50)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_unconditional_refresh_is_not_cancelled_by_stale_volume_target(self):
        self.w.poll_running = True
        self.p.request()
        self.p.request(10, lambda: False)
        self.w.poll_running = False
        self.p.finished()
        QTest.qWait(20)
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

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
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_fresh_groups_skip_only_until_half_interval_expires(self):
        self.p.observed('volume', 100)
        self.p.observed('inputs', 100.2)
        with patch.object(REMOTE['time'], 'monotonic', return_value=100.5):
            self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'power'})
        self.w.poll_status.reset_mock()
        with patch.object(REMOTE['time'], 'monotonic', return_value=101):
            self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'power', 'volume'})

    def test_scheduled_volume_read_does_not_suppress_other_groups(self):
        self.p.request(500, groups={'volume'})
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'power', 'inputs'})
        self.assertNotIn('volume', self.p._observed)

    def test_command_queue_reservation_defers_only_affected_groups(self):
        self.p.hold({'volume'})
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups={'power', 'inputs'})
        self.p.release({'volume'})
        self.p.request(10, groups={'volume'})
        self.w.poll_status.reset_mock()
        QTest.qWait(30)
        self.w.poll_status.assert_called_once_with(groups={'volume'})

    def test_explicit_refresh_bypasses_freshness_and_failure_releases_group(self):
        self.p.observed('volume', REMOTE['time'].monotonic())
        self.p.request(groups={'volume'})
        self.w.poll_status.assert_called_once_with(groups={'volume'})
        # No successful response is delivered; a periodic tick must retry.
        self.w.poll_status.reset_mock()
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_cancelled_conditional_refresh_does_not_mark_fresh(self):
        self.p.request(10, lambda: False, groups={'volume'})
        QTest.qWait(30)
        self.w.poll_status.assert_not_called()
        self.assertFalse(self.p.pending)
        self.assertIn('volume', self.p.periodic_groups())

    def test_calibration_bypasses_freshness(self):
        now = REMOTE['time'].monotonic()
        for group in self.p.GROUPS:
            self.p.observed(group, now)
        self.w.calibration_active = True
        self.p._periodic_due()
        self.w.poll_status.assert_called_once_with(groups=self.p.GROUPS)

    def test_event_refresh_preserves_periodic_timer_deadline(self):
        self.p.update()
        QTest.qWait(30)
        remaining = self.p.periodic.remainingTime()
        self.p.update(refresh=True)
        self.assertLessEqual(self.p.periodic.remainingTime(), remaining)

    def test_group_deadlines_remain_independent(self):
        self.p.request(10, groups={'volume'})
        self.p.request(100, groups={'inputs'})
        QTest.qWait(40)
        self.w.poll_status.assert_called_once_with(groups={'volume'})
        self.assertEqual(set(self.p._scheduled), {'inputs'})
        QTest.qWait(100)
        self.assertEqual(self.w.poll_status.call_count, 2)
        self.assertEqual(self.w.poll_status.call_args.kwargs['groups'], {'inputs'})

    def test_input_refresh_waits_for_previous_input_request(self):
        self.w._input_status_running = True
        self.p.request(groups={'inputs'})
        self.w.poll_status.assert_not_called()
        self.assertTrue(self.p.pending)
        self.w._input_status_running = False
        self.p.finished()
        QTest.qWait(10)
        self.w.poll_status.assert_called_once_with(groups={'inputs'})

    def test_rejected_command_releases_reserved_group(self):
        self.w.run_worker = Mock(return_value=False)
        accepted = REMOTE['SonyRemote'].run_command(
            self.w, Mock(), refresh_groups={'volume'})
        self.assertFalse(accepted)
        self.assertFalse(self.p._commands['volume'])
        self.assertFalse(self.p.pending)
        self.assertIn('volume', self.p.periodic_groups())

    def test_latest_queued_command_gets_full_settling_delay(self):
        self.w.run_worker = Mock(return_value=True)
        for _ in range(2):
            REMOTE['SonyRemote'].run_command(
                self.w, Mock(), refresh_groups={'volume'}, refresh_delay_ms=40)
        finishes = [call.args[3] for call in self.w.run_worker.call_args_list]
        finishes[0]()
        QTest.qWait(60)
        self.w.poll_status.assert_not_called()
        finishes[1]()
        self.p._periodic_due()
        self.assertNotIn('volume', self.w.poll_status.call_args.kwargs['groups'])
        self.w.poll_status.reset_mock()
        QTest.qWait(10)
        self.w.poll_status.assert_not_called()
        QTest.qWait(60)
        self.w.poll_status.assert_called_once_with(groups={'volume'})
