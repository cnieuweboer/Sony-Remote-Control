"""Wire contract examples duplicated in both repos; no shared runtime module."""
import io
import json
from pathlib import Path
import runpy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
IS_TV = (ROOT / 'SonyRemote.pyw').exists()
REMOTE = runpy.run_path(str(ROOT / ('SonyRemote.pyw' if IS_TV else 'YamahaRemote.pyw')),
                       run_name='amplifier_protocol_tests')
FIXTURE = json.loads((ROOT / 'tests/fixtures/amplifier_protocol_v1.json').read_text())


class MemorySocket:
    def __init__(self, message):
        self.data = bytearray(REMOTE['_ipc_encode'](message))
        self.abort = Mock()

    def bytesAvailable(self):
        return len(self.data)

    def read(self, count):
        result = bytes(self.data[:count])
        del self.data[:count]
        return result


class WireContractTests(unittest.TestCase):
    def test_every_fixture_passes_real_frame_parser(self):
        for group in FIXTURE.values():
            for message in group:
                self.assertEqual(REMOTE['_ipc_frames'](MemorySocket(message), bytearray()), [message])

    @unittest.skipIf(IS_TV, 'Amplifier producer only')
    def test_real_snapshot_matches_state_fixtures(self):
        for expected in FIXTURE['states']:
            config = REMOTE['_default_config_copy']()
            config.update(Host='127.0.0.1', VolumeDb=expected['volume_unit'] == 'dB',
                          GlobalVolumeHotkeys=True)
            window = SimpleNamespace(config=config, last_status={
                'power':'on', 'input':'tv', 'volume':100, 'max_volume':154, 'mute':False},
                _last_status_at=100, connected=True, _shutting_down=False,
                global_volume_hotkeys_active=True, calibration_active=False,
                api=REMOTE['YamahaApi']('127.0.0.1', config))
            with patch.object(REMOTE['time'], 'monotonic', return_value=100):
                actual = REMOTE['YamahaRemote']._amplifier_snapshot(window)
            self.assertEqual(actual, expected)

    @unittest.skipIf(IS_TV, 'Amplifier producer only')
    def test_real_command_handler_produces_fixture_acknowledgements(self):
        for command in FIXTURE['commands']:
            for ack in FIXTURE['acks']:
                if 'reason' not in ack:
                    continue  # Legacy senders may omit this optional field.
                socket = MemorySocket(command)
                server = SimpleNamespace(clients={socket:bytearray()},
                    command=Mock(return_value=(ack['accepted'], ack['reason'])), _send=Mock())
                REMOTE['AmplifierServer']._read(server, socket)
                server.command.assert_called_once_with(command['command'], command['input'])
                server._send.assert_called_once_with(socket, dict(ack, id=command['id']))
                socket.abort.assert_not_called()


@unittest.skipUnless(IS_TV, 'TV consumer only')
class ClientContractTests(unittest.TestCase):
    def client(self, message, *, pending=True):
        return SimpleNamespace(socket=MemorySocket(message), buffer=bytearray(),
            state=None, state_deadline=0, lease=Mock(), state_changed=Mock(),
            pending={1:Mock()} if pending else {}, _lost=Mock(),
            _validate_state=REMOTE['AmplifierClient']._validate_state,
            command_failed=Mock(), command_succeeded=Mock())

    def read(self, client):
        REMOTE['AmplifierClient']._read(client)

    def test_real_client_accepts_all_state_fixtures(self):
        for message in FIXTURE['states']:
            client = self.client(message)
            self.read(client)
            client.socket.abort.assert_not_called()
            client.state_changed.emit.assert_called_once_with(message)

    def test_optional_reasons_and_silent_queue_overflow(self):
        for message in FIXTURE['acks']:
            client = self.client(message)
            timer = client.pending[1]
            self.read(client)
            client.socket.abort.assert_not_called()
            timer.stop.assert_called_once()
            timer.deleteLater.assert_called_once()
            self.assertFalse(client.pending)
            if message['accepted']:
                client.command_succeeded.emit.assert_called_once_with()
                client.command_failed.emit.assert_not_called()
            elif message.get('reason') == 'queue_full':
                client.command_succeeded.emit.assert_not_called()
                client.command_failed.emit.assert_not_called()
            else:
                client.command_failed.emit.assert_called_once()
                self.assertIn('not retried', client.command_failed.emit.call_args.args[0])
                if 'reason' not in message:
                    self.assertNotIn('state changed', client.command_failed.emit.call_args.args[0])

    def test_unsolicited_success_cannot_clear_feedback(self):
        client = self.client(FIXTURE['acks'][0], pending=False)
        self.read(client)
        client.command_succeeded.emit.assert_not_called()

    def test_malformed_protocol_is_reported_even_without_pending_commands(self):
        bad_messages = [dict(FIXTURE['acks'][0], reason=None),
                        dict(FIXTURE['acks'][0], accepted=1),
                        dict(FIXTURE['states'][0], volume_unit='invalid'),
                        {'version':2, 'type':'state'}]
        function_globals = REMOTE['AmplifierClient']._read.__globals__
        for message in bad_messages:
            client = self.client(message, pending=False)
            with patch.dict(function_globals, record_exception=Mock()) as patched:
                self.read(client)
                patched['record_exception'].assert_called_once()
                self.assertTrue(patched['record_exception'].call_args.kwargs['deduplicate'])
            client.socket.abort.assert_called_once()
            client._lost.assert_called_once()
            self.assertIn('Incompatible amplifier protocol', client.command_failed.emit.call_args.args[0])

    def test_repeated_protocol_failures_are_deduplicated_in_diagnostics(self):
        # Exercise recording without opening a runtime log or installing hooks.
        diagnostics = object.__new__(REMOTE['Diagnostics'])
        stream = io.StringIO()
        diagnostics._stream = stream
        diagnostics._lock = threading.RLock()
        diagnostics._seen = set()
        function_globals = REMOTE['record_exception'].__globals__
        with patch.dict(function_globals, _diagnostics=diagnostics):
            for _ in range(3):
                self.read(self.client(dict(FIXTURE['acks'][0], reason=None)))
        self.assertEqual(stream.getvalue().count('Amplifier protocol:'), 1)


@unittest.skipUnless(IS_TV, 'TV feedback only')
class FeedbackTests(unittest.TestCase):
    def test_feedback_survives_discovery_refresh_until_success(self):
        cls = REMOTE['SonyRemote']
        window = SimpleNamespace(_discovery_errors={'apps':'Unavailable'},
                                 _amplifier_error='', power_button=Mock(), app_combo=Mock(), input_combo=Mock())
        window._update_discovery_feedback = lambda: cls._update_discovery_feedback(window)
        cls._show_amplifier_error(window, 'Command timed out.')
        window._update_discovery_feedback()
        text = window.power_button.setToolTip.call_args.args[0]
        self.assertIn('Apps unavailable', text)
        self.assertIn('Command timed out.', text)
        window._discovery_errors.clear()
        window._update_discovery_feedback()
        self.assertIn('Command timed out.', window.power_button.setToolTip.call_args.args[0])
        window._discovery_errors['inputs'] = 'Unavailable'
        cls._clear_amplifier_error(window)
        text = window.power_button.setToolTip.call_args.args[0]
        self.assertNotIn('Command timed out.', text)
        self.assertIn('Inputs unavailable', text)


if __name__ == '__main__':
    unittest.main()
