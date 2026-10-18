"""Device-free regressions for stream reads, config snapshots, and Win32 mutexes."""
import hashlib
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
REMOTE = runpy.run_path(str(ROOT / 'SonyRemote.pyw'), run_name='reliability_tests')
GLOBALS = REMOTE['_ipc_frames'].__globals__
IS_TV = 'SonyRemote' in REMOTE


class MemorySocket:
    def __init__(self, data=b'', read_limit=4097):
        self.data = bytearray(data)
        self.read_limit = read_limit
        self.reads = 0

    def bytesAvailable(self):
        return len(self.data)

    def read(self, count):
        self.reads += 1
        count = min(count, self.read_limit, len(self.data))
        data = self.data[:count]
        del self.data[:count]
        return bytes(data)


class FrameTests(unittest.TestCase):
    def test_fragmented_utf8_frame_retains_partial_bytes(self):
        message = {'version': 1, 'name': 'café'}
        payload = json.dumps(message, ensure_ascii=False).encode('utf-8') + b'\n'
        split = payload.index('é'.encode('utf-8')) + 1
        socket = MemorySocket(payload[:split], read_limit=3)
        buffer = bytearray()
        self.assertEqual(REMOTE['_ipc_frames'](socket, buffer), [])
        self.assertEqual(buffer, payload[:split])
        socket.data.extend(payload[split:])
        self.assertEqual(REMOTE['_ipc_frames'](socket, buffer), [message])
        self.assertFalse(buffer)

    def test_drains_large_burst_of_individually_valid_frames_in_order(self):
        messages = [{'version': 1, 'id': index} for index in range(500)]
        socket = MemorySocket(b''.join(REMOTE['_ipc_encode'](message) for message in messages))
        self.assertEqual(REMOTE['_ipc_frames'](socket, bytearray()), messages)
        self.assertFalse(socket.bytesAvailable())
        self.assertGreater(socket.reads, 1)

    def test_complete_and_partial_oversized_frames_are_rejected(self):
        for payload in (b'x' * 4097, b'x' * 4097 + b'\n', b' ' * 4097 + b'{"version":1}\n'):
            with self.subTest(length=len(payload)):
                with self.assertRaisesRegex(ValueError, 'too large'):
                    REMOTE['_ipc_frames'](MemorySocket(payload, read_limit=17), bytearray())

    def test_exact_frame_limit_is_accepted_across_read_boundaries(self):
        prefix = b'{"version":1}'
        payload = prefix + b' ' * (REMOTE['IPC_MAX_FRAME'] - len(prefix)) + b'\n'
        self.assertEqual(REMOTE['_ipc_frames'](MemorySocket(payload, read_limit=17), bytearray()), [{'version': 1}])

    def test_invalid_messages_are_rejected(self):
        for payload in (b'not json\n', b'[]\n', b'{"version":true}\n', b'{"version":2}\n'):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    REMOTE['_ipc_frames'](MemorySocket(payload), bytearray())

    def test_empty_read_does_not_spin(self):
        socket = MemorySocket(b'x', read_limit=0)
        self.assertEqual(REMOTE['_ipc_frames'](socket, bytearray()), [])
        self.assertEqual(socket.reads, 1)


class ChangingPath:
    def __init__(self, content, later=b'not the same config'):
        self.content = content
        self.later = later
        self.reads = 0

    def read_bytes(self):
        self.reads += 1
        return self.content if self.reads == 1 else self.later

    def read_text(self, **kwargs):
        raise AssertionError('Config must be parsed from the byte snapshot')

    def open(self, *args, **kwargs):
        raise AssertionError('Config must not be reopened for parsing')

    def with_name(self, name):
        return self


class ConfigSnapshotTests(unittest.TestCase):
    def config_bytes(self):
        config = REMOTE['_default_config_copy']()
        config['Host'] = '192.168.1.100'
        if IS_TV:
            config['PreSharedKey'] = 'test-only'
        return json.dumps(config).encode('utf-8')

    def remote(self):
        self.restarts = []
        return SimpleNamespace(calibration_active=False, _shutting_down=False, _config_restart_pending=False,
                               _config_digest=b'previous', _invalid_config_digest=None,
                               _reload_volume_mapping=lambda: None, _watch_config_paths=lambda: None,
                               _restart_for_config_change=lambda: self.restarts.append(True))

    def check(self, remote):
        cls = REMOTE['SonyRemote' if IS_TV else 'YamahaRemote']
        cls._check_external_config_change(remote)

    def test_hash_and_validation_use_one_snapshot_even_if_file_changes(self):
        content = self.config_bytes()
        path = ChangingPath(content)
        remote = self.remote()
        validate = Mock(wraps=REMOTE['validate_config'])
        with patch.dict(GLOBALS, {'CONFIG_PATH': path, 'validate_config': validate}):
            self.check(remote)
        self.assertEqual(path.reads, 1)
        self.assertEqual(validate.call_args.args[0]['Host'], '192.168.1.100')
        self.assertEqual(self.restarts, [True])
        self.assertIsNone(remote._invalid_config_digest)

    def test_invalid_snapshot_keeps_instance_and_caches_its_own_hash(self):
        content = b'{"Host":"300.1.1.1"}'
        path = ChangingPath(content, later=content)
        remote = self.remote()
        with patch.dict(GLOBALS, {'CONFIG_PATH': path}), patch.object(REMOTE['QMessageBox'], 'warning') as warning:
            self.check(remote)
            self.check(remote)
        self.assertEqual(path.reads, 2)  # Exactly one read per file-change check.
        self.assertEqual(remote._invalid_config_digest, hashlib.sha256(content).digest())
        self.assertFalse(self.restarts)
        self.assertEqual(warning.call_count, 1)

    def test_unchanged_snapshot_does_not_restart(self):
        content = self.config_bytes()
        remote = self.remote()
        remote._config_digest = hashlib.sha256(content).digest()
        path = ChangingPath(content)
        with patch.dict(GLOBALS, {'CONFIG_PATH': path}):
            self.check(remote)
        self.assertEqual(path.reads, 1)
        self.assertFalse(self.restarts)

    def test_unreadable_snapshot_keeps_instance_running(self):
        remote = self.remote()
        path = SimpleNamespace(read_bytes=Mock(side_effect=OSError('unreadable')))
        with patch.dict(GLOBALS, {'CONFIG_PATH': path}), patch.object(REMOTE['QMessageBox'], 'warning') as warning:
            self.check(remote)
        self.assertFalse(self.restarts)
        warning.assert_not_called()


class MutexTests(unittest.TestCase):
    def exercise(self, handle, error):
        cached = [183]  # A stale error must be cleared before CreateMutexW.
        events = []
        def set_error(value):
            cached[0] = value
            events.append(('set', value))
        def create(*args):
            events.append(('create', cached[0]))
            cached[0] = error
            return handle
        def get_error():
            events.append(('get', cached[0]))
            return cached[0]
        kernel = SimpleNamespace(CreateMutexW=Mock(side_effect=create), CloseHandle=Mock(return_value=True))
        ctypes = REMOTE['ctypes']
        globals_patch = {'sys': SimpleNamespace(platform='win32')}
        if IS_TV:
            globals_patch.update(_instance_mutex_handle=None, _instance_mutex_api=None)
        with patch.dict(GLOBALS, globals_patch), \
             patch.object(ctypes, 'WinDLL', return_value=kernel, create=True) as load, \
             patch.object(ctypes, 'set_last_error', side_effect=set_error, create=True), \
             patch.object(ctypes, 'get_last_error', side_effect=get_error, create=True):
            if not handle:
                with self.assertRaises(OSError) as failure:
                    if IS_TV:
                        REMOTE['acquire_single_instance']()
                    else:
                        REMOTE['_WindowsSingleInstanceMutex']('test-instance')
                self.assertEqual(failure.exception.errno, error)
                kernel.CloseHandle.assert_not_called()
            else:
                if IS_TV:
                    acquired = REMOTE['acquire_single_instance']()
                    close = REMOTE['release_single_instance']
                else:
                    mutex = REMOTE['_WindowsSingleInstanceMutex']('test-instance')
                    acquired, close = mutex.acquired, mutex.close
                self.assertEqual(acquired, error != 183)
                close()
                close()  # Release remains idempotent.
                kernel.CloseHandle.assert_called_once_with(handle)
            load.assert_called_once_with('kernel32', use_last_error=True)
        self.assertEqual(events[:3], [('set', 0), ('create', 0), ('get', error)])

    def test_new_mutex_preserves_pointer_sized_handle_for_release(self):
        self.exercise(2 ** 40, 0)

    def test_existing_mutex_detected_from_cached_last_error(self):
        self.exercise(2 ** 40, 183)

    def test_failed_creation_reports_cached_error(self):
        self.exercise(0, 5)


class MappingSnapshotTests(unittest.TestCase):
    def test_mapping_reload_uses_one_snapshot_and_retains_last_good_mapping(self):
        content = json.dumps({'format_version': 2, 'tv_device_id': 'tv', 'amplifier_device_id': 'amp',
                              'amplifier_tv_input': 'tv', 'tv_volume_by_amplifier_raw_volume': [0, 0, 1]}).encode()
        path = ChangingPath(content, later=b'invalid edit')
        remote = SimpleNamespace(_mapping_digest=None, volume_mapping=None, amplifier_state=None,
                                 _amplifier_state_changed=Mock())
        reload_mapping = REMOTE['SonyRemote']._reload_volume_mapping
        with patch.dict(GLOBALS, {'CONFIG_PATH': path}):
            reload_mapping(remote)
            self.assertEqual(path.reads, 1)
            self.assertEqual(remote.volume_mapping['lookup'][2]['tv'], 1)
            previous = remote.volume_mapping
            reload_mapping(remote)
        self.assertEqual(path.reads, 2)
        self.assertIs(remote.volume_mapping, previous)
        remote._amplifier_state_changed.assert_called_once_with(None)
        self.assertEqual(remote._mapping_digest, hashlib.sha256(b'invalid edit').digest())


if __name__ == '__main__':
    unittest.main()
