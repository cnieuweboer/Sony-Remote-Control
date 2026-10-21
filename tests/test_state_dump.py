"""Cached diagnostic reports: no device requests or private configuration."""
import json
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BRAND = 'Sony' if (ROOT / 'SonyRemote.pyw').exists() else 'Yamaha'
R = runpy.run_path(str(ROOT / (BRAND + 'Remote.pyw')), run_name='state_dump_tests')

class DumpTests(unittest.TestCase):
    def test_history_is_bounded_and_omits_exception_messages(self):
        history = R['RequestHistory']()
        history.record('status')
        history.record('status', ValueError('SECRET_PAYLOAD'))
        for i in range(100):
            history.record(str(i), RuntimeError('SECRET_PAYLOAD'))
        result = history.snapshot(time.monotonic())
        self.assertEqual(set(result), {'status', 'background'})
        self.assertEqual(result['status']['latest'], 'failure')
        self.assertIn('success', result['status'])
        self.assertEqual(result['status']['failure']['exception_type'], 'ValueError')
        self.assertNotIn('SECRET_PAYLOAD', json.dumps(result))
        self.assertNotIn('observed_at', json.dumps(result))

    def test_writer_unique_complete_and_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            blocked = root / 'blocked'
            blocked.write_text('not a directory')
            snapshot = {'mode': 'cached', 'value': 42}
            first = R['write_state_dump'](snapshot, [blocked, root])
            second = R['write_state_dump'](snapshot, [root])
            self.assertTrue(first['fallback'])
            self.assertNotEqual(first['path'], second['path'])
            self.assertEqual(json.loads(Path(first['path']).read_text()), snapshot)
            self.assertTrue(Path(second['path']).is_absolute())

    def test_busy_does_not_capture_or_enqueue_another_dump(self):
        dumper = R['StateDumper'](None, Mock())
        dumper.worker = object()
        done = Mock()
        dumper.request(done)
        done.assert_called_once_with({'ok': False, 'error': 'busy'})
        dumper.snapshot.assert_not_called()

    def test_cli_dispatch_precedes_configuration_and_lock(self):
        function = R['_run_application']
        with patch.dict(function.__globals__, {'QApplication': Mock(), 'dump_existing_state': Mock(return_value=0),
                'acquire_single_instance': Mock(side_effect=AssertionError('lock acquired'))}), \
             patch('sys.argv', ['remote.pyw', '--dump-state']):
            self.assertEqual(function(), 0)
            function.__globals__['dump_existing_state'].assert_called_once_with('tv' if BRAND == 'Sony' else 'amplifier')

    def test_real_window_snapshot_is_cached_and_private(self):
        if BRAND == 'Sony':
            from test_graceful_failures import DiscoveryTests
            fixture = DiscoveryTests()
            fixture.setUpClass()
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            fixture.start_and_settle()
            window = fixture.window
        else:
            from test_volume_controls import VolumeControlTests
            fixture = VolumeControlTests()
            fixture.setUpClass()
            self.addCleanup(fixture.doCleanups)
            window = fixture.window()
            window.connected = True
            window.last_status = {"power": "on", "input": "tv", "volume": 51, "max_volume": 154, "mute": False}
            window._last_status_at = time.monotonic()
        window.config['Host'] = 'SECRET_HOST'
        window.config['PreSharedKey'] = 'SECRET_PSK'
        window.run_worker = Mock(side_effect=AssertionError('Unexpected device work'))
        visible = window.isVisible()
        report = window._cached_state()
        payload = json.dumps(report, allow_nan=False)
        self.assertEqual(report['mode'], 'cached')
        self.assertFalse(report['fresh_queries_performed'])
        self.assertNotIn('SECRET_HOST', payload)
        self.assertNotIn('SECRET_PSK', payload)
        window.run_worker.assert_not_called()
        self.assertEqual(window.isVisible(), visible)
