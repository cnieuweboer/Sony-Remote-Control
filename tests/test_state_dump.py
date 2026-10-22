"""Cached diagnostic reports: no device requests or private configuration."""
import json
from pathlib import Path
import runpy
import tempfile
import time
import unittest
from unittest.mock import MagicMock, Mock, patch

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
        window.api.polling_responses = R['PollingResponses'](('status',))
        window.api.polling_responses.record('status', {'power': 'on', 'extra': 42})
        report = window._cached_state()
        full = window._cached_state(full=True)
        self.assertNotIn('polling_responses', report)
        self.assertEqual(full['mode'], 'cached_full')
        self.assertEqual(full['polling_responses']['status']['response']['extra'], 42)
        self.assertFalse(full['fresh_queries_performed'])
        self.assertEqual(report['polling_counters'], full['polling_counters'])
        self.assertEqual(report['polling_counters']['status']['successes'], 1)
        payload = json.dumps(report, allow_nan=False)
        self.assertEqual(report['mode'], 'cached')
        self.assertFalse(report['fresh_queries_performed'])
        self.assertNotIn('SECRET_HOST', payload)
        self.assertNotIn('SECRET_PSK', payload)
        window.run_worker.assert_not_called()
        self.assertEqual(window.isVisible(), visible)

    def test_polling_cache_is_detached_bounded_and_timestamped(self):
        cache = R['PollingResponses'](('status', 'inventory'))
        response = {'result': [{'extra_device_field': 12}]}
        cache.record('status', response)
        response['result'][0]['extra_device_field'] = 99
        cache.record('setTextForm', {'text': 'SECRET'})
        snapshot = cache.snapshot(time.monotonic())
        self.assertIsNone(snapshot['inventory'])
        self.assertEqual(snapshot['status']['response']['result'][0]['extra_device_field'], 12)
        self.assertIn('at_utc', snapshot['status'])
        self.assertGreaterEqual(snapshot['status']['age_seconds'], 0)
        self.assertNotIn('SECRET', json.dumps(snapshot))
        snapshot['status']['response'].clear()
        self.assertTrue(cache.snapshot(time.monotonic())['status']['response'])

    def test_actual_api_caches_reads_but_not_commands_or_failed_responses(self):
        api = R[BRAND + 'Api']('device.local', 'SECRET_PSK') if BRAND == 'Sony' else R['YamahaApi']('device.local')
        success = {'result': [{'status': 'active'}], 'extra': 'preserved'} if BRAND == 'Sony' else {'response_code': 0, 'power': 'on', 'extra': 'preserved'}
        globals_ = R[BRAND + 'Api'].__init__.__globals__
        reply = Mock()
        reply.read.return_value = json.dumps(success).encode()
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = reply
        read_key = 'system/getPowerStatus' if BRAND == 'Sony' else 'main/getStatus'
        with patch.dict(globals_, {'DEVICE_HTTP_OPENER': opener}):
            api.get_power() if BRAND == 'Sony' else api.get_status()
            api.rpc('appControl', 'setTextForm', ['SECRET_TEXT']) if BRAND == 'Sony' else api._get_json('main/setVolume?volume=20')
            reply.read.return_value = json.dumps({'error': [1, 'failed']} if BRAND == 'Sony' else {'response_code': 1}).encode()
            with self.assertRaises(R['DeviceResponseError']):
                api.get_power() if BRAND == 'Sony' else api.get_status()
        report = api.polling_responses.snapshot(time.monotonic())
        self.assertEqual(report[read_key]['response'], success)
        self.assertEqual(sum(value is not None for value in report.values()), 1)
        self.assertNotIn('SECRET', json.dumps(report))

    def test_full_cli_dispatch(self):
        function = R['_run_application']
        with patch.dict(function.__globals__, {'QApplication': Mock(), 'dump_existing_state': Mock(return_value=0)}), \
             patch('sys.argv', ['remote.pyw', '--dump-state-full']):
            self.assertEqual(function(), 0)
            function.__globals__['dump_existing_state'].assert_called_once_with('tv' if BRAND == 'Sony' else 'amplifier', full=True)

    def test_full_save_confirmation_warns_before_sharing(self):
        function = R['dump_existing_state']
        for full in (False, True):
            for console in (False, True):
                with self.subTest(full=full, console=console):
                    dialogs = Mock()
                    printer = Mock()
                    with patch.dict(function.__globals__, {
                            '_request_existing_instance': Mock(return_value={'ok': True, 'path': '/tmp/report.json'}),
                            'QMessageBox': dialogs, 'print': printer}), \
                         patch('sys.stdout', Mock() if console else None):
                        self.assertEqual(function('tv', full=full), 0)
                    message = printer.call_args.args[0] if console else dialogs.information.call_args.args[2]
                    self.assertIn('/tmp/report.json', message)
                    self.assertEqual('Privacy warning:' in message, full)
                    if full:
                        self.assertIn('Review it before sharing', message)

    def test_session_counters_recovery_empty_and_detached_snapshot(self):
        key = 'appControl/getApplicationList' if BRAND == 'Sony' else 'system/getFeatures'
        cache = R['PollingResponses']((key, 'status'))
        empty = {'result': [[]]} if BRAND == 'Sony' else {'system': {'input_list': []}}
        cache.failed(key, 'timeouts')
        cache.failed(key, 'malformed_responses')
        cache.record(key, empty)
        cache.record(key, empty)
        cache.failed('setTextForm', 'http_errors')
        counters = cache.counters()
        self.assertEqual(counters[key]['successes'], 2)
        self.assertEqual(counters[key]['empty_inventories'], 2)
        self.assertEqual(counters[key]['failures'], 2)
        self.assertEqual(counters[key]['timeouts'], 1)
        self.assertEqual(counters[key]['connection_failures'], 1)
        self.assertEqual(counters[key]['recoveries'], 1)
        self.assertEqual(counters[key]['consecutive_failures'], 0)
        counters[key]['successes'] = 99
        self.assertEqual(cache.counters()[key]['successes'], 2)
        self.assertTrue(all(value == 0 for value in cache.counters()['status'].values()))
        self.assertTrue(all(value == 0 for value in R['PollingResponses']((key,)).counters()[key].values()))

    def test_api_failure_categories_and_recovery_preserve_exceptions(self):
        from urllib.error import URLError, HTTPError
        api = R[BRAND + 'Api']('device.local', 'SECRET') if BRAND == 'Sony' else R['YamahaApi']('device.local')
        key = 'system/getPowerStatus' if BRAND == 'Sony' else 'main/getStatus'
        read = api.get_power if BRAND == 'Sony' else api.get_status
        opener = MagicMock()
        globals_ = R[BRAND + 'Api'].__init__.__globals__
        success = {'result': [{'status': 'active'}]} if BRAND == 'Sony' else {'response_code': 0, 'power': 'on'}
        malformed = {'result': 12} if BRAND == 'Sony' else {'response_code': 'bad'}
        rejected = {'error': [1, 'SECRET']} if BRAND == 'Sony' else {'response_code': 1}
        with patch.dict(globals_, {'DEVICE_HTTP_OPENER': opener}):
            for error in (URLError(TimeoutError('SECRET')), URLError(OSError('SECRET')), HTTPError('url', 503, 'SECRET', {}, None)):
                opener.open.side_effect = error
                with self.assertRaises(R['DeviceConnectionError'] if not isinstance(error, HTTPError) else R['DeviceResponseError']):
                    read()
            opener.open.side_effect = None
            reply = opener.open.return_value.__enter__.return_value
            for payload in (b'bad json', json.dumps(malformed).encode(), json.dumps(rejected).encode()):
                reply.read.return_value = payload
                with self.assertRaises(R['DeviceResponseError']):
                    read()
            reply.read.return_value = json.dumps(success).encode()
            read()
            read()
        counters = api.polling_responses.counters()[key]
        self.assertEqual(counters['successes'], 2)
        self.assertEqual(counters['failures'], 6)
        self.assertEqual(counters['malformed_responses'], 2)
        self.assertEqual(counters['connection_failures'], 2)
        self.assertEqual(counters['timeouts'], 1)
        self.assertEqual(counters['http_errors'], 1)
        self.assertEqual(counters['device_rejections'], 1)
        self.assertEqual(counters['recoveries'], 1)
        self.assertNotIn('SECRET', json.dumps(counters))

    def test_group_counts_first_recovery_only(self):
        history = R['RequestHistory']()
        history.record('status', ValueError())
        history.record('status', ValueError())
        history.record('status')
        history.record('status')
        counts = history.snapshot(time.monotonic())['status']
        self.assertEqual(counts['successes'], 2)
        self.assertEqual(counts['failures'], 2)
        self.assertEqual(counts['recoveries'], 1)
        self.assertEqual(counts['consecutive_failures'], 0)
