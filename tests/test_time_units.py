"""Boundary conversions keep internal ages/deadlines in seconds."""
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

REMOTE = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'SonyRemote.pyw'),
                        run_name='time_unit_tests')


class TimeUnitsTests(unittest.TestCase):
    def test_remaining_validity_clamps_expired_and_future_observations(self):
        validity = REMOTE['remaining_validity_ms']
        for age, expected in ((-1, 6000), (0, 6000), (0.5, 5500), (6, 0), (7, 0)):
            with self.subTest(age=age):
                self.assertEqual(validity(age, 6.0), expected)

    def test_validity_uses_supplied_age_limit_and_whole_wire_milliseconds(self):
        validity = REMOTE['remaining_validity_ms']
        self.assertEqual(validity(0.75, 2.5), 1750)
        self.assertEqual(validity(0.0005, 1.0), 999)
        self.assertEqual(validity(0, 0), 0)

    def test_activation_retries_share_seconds_budget_and_qt_receives_milliseconds(self):
        activate = REMOTE['activate_existing_instance']
        now = [100.0]
        sleeps = []
        socket = Mock()
        def wait(milliseconds):
            self.assertIsInstance(milliseconds, int)
            self.assertGreaterEqual(milliseconds, 0)
            self.assertLessEqual(milliseconds, 150)
            now[0] += milliseconds / 1000
            return False
        def sleep(seconds):
            self.assertGreaterEqual(seconds, 0)
            self.assertLessEqual(seconds, 0.05)
            sleeps.append(seconds)
            now[0] += seconds
        socket.waitForConnected.side_effect = wait
        clock = SimpleNamespace(monotonic=lambda: now[0], sleep=sleep)
        with patch.dict(activate.__globals__, time=clock, QLocalSocket=Mock(return_value=socket)):
            self.assertFalse(activate('tv', timeout_seconds=0.22))
        self.assertAlmostEqual(now[0] - 100, 0.22)
        self.assertGreaterEqual(socket.connectToServer.call_count, 2)
        self.assertTrue(sleeps)
        socket.abort.assert_called()
