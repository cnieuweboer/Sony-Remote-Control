"""Validate real decoded read responses before counting success or recovery."""
import copy
import json
from pathlib import Path
import runpy
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
BRAND = 'Sony' if (ROOT / 'SonyRemote.pyw').exists() else 'Yamaha'
R = runpy.run_path(str(ROOT / (BRAND + 'Remote.pyw')), run_name='poll_validation_tests')


class PollResponseValidationTests(unittest.TestCase):
    def api(self):
        return R['SonyApi']('device.local', 'test-only') if BRAND == 'Sony' else R['YamahaApi']('device.local')

    def cases(self):
        if BRAND == 'Sony':
            audio = {'target': 'speaker', 'volume': 20, 'mute': False, 'minVolume': 0, 'maxVolume': 100}
            return [
                ('system/getPowerStatus', lambda api: api.get_power(),
                 {'result': [{'status': 'active'}]},
                 [{'result': []}, {'result': [None]}, {'result': [{}]}, {'result': [{'status': []}]}]),
                ('audio/getVolumeInformation', lambda api: api.get_volume(), {'result': [[audio]]},
                 [{'result': []}, {'result': [None]}, {'result': [[None]]},
                  *({'result': [[dict(audio, **change)]]} for change in
                    ({'volume': True}, {'volume': '20'}, {'mute': 'false'}, {'maxVolume': None}, {'minVolume': 30}))]),
                ('appControl/getApplicationList', lambda api: api.get_apps(),
                 {'result': [[{'uri': 'app', 'title': 'App'}]]},
                 [{'result': []}, {'result': [None]}, {'result': [[None]]},
                  {'result': [[{'uri': 'app'}]]},
                  {'result': [[{'uri': 'app', 'title': 'App'}, {'uri': 'app', 'title': 'Duplicate'}]]}]),
                ('avContent/getCurrentExternalInputsStatus', lambda api: api.get_inputs(),
                 {'result': [[{'uri': 'input', 'title': 'Input', 'status': 'true'}]]},
                 [{'result': []}, {'result': [None]}, {'result': [[{'title': 'Input'}]]}]),
                ('system/getRemoteControllerInfo', lambda api: api.load_remote_codes(),
                 {'result': [{}, [{'name': 'Up', 'value': 'code'}]]},
                 [{'result': []}, {'result': [{}]}, {'result': [{}, None]},
                  {'result': [{}, [None]]}, {'result': [{}, [{'name': 'Up', 'value': 1}]]}]),
                ('appControl/getApplicationStatusList', lambda api: api.is_text_input_active(),
                 {'result': [[{'name': 'textInput', 'status': 'on'}]]},
                 [{'result': []}, {'result': [None]}, {'result': [[None]]}, {'result': [[{'name': 'textInput'}]]}]),
            ]
        status = {'response_code': 0, 'power': 'on', 'volume': 50, 'mute': False, 'input': 'tv', 'max_volume': 154}
        return [
            ('main/getStatus', lambda api: api.get_status(), status,
             [{'response_code': 0}, dict(status, power=None), dict(status, volume=True),
              dict(status, volume='50'), dict(status, volume=float('nan')),
              dict(status, max_volume=-1), dict(status, mute=1), dict(status, input=[]),
              {'response_code': 0, 'power': 'on'}]),
            ('system/getFeatures', lambda api: api._get_features(refresh=True),
             {'response_code': 0, 'system': {'input_list': [{'id': 'tv'}], 'func_list': ['speaker_a']}},
             [{'response_code': 0}, {'response_code': 0, 'system': {}},
              *({'response_code': 0, 'system': {'input_list': entries}} for entries in
                (None, 'bad', [None], [{}], [{'id': 'tv'}, {'id': 'tv'}])),
              {'response_code': 0, 'system': {'input_list': [], 'func_list': [None]}}]),
            ('system/getFuncStatus', lambda api: api.get_speaker_state(('a', 'b')),
             {'response_code': 0, 'speaker_a': True, 'speaker_b': False},
             [{'response_code': 0}, {'response_code': 0, 'speaker_a': True},
              {'response_code': 0, 'speaker_a': 1, 'speaker_b': False},
              {'response_code': 0, 'speaker_a': True, 'speaker_b': 'false'}]),
            ('system/getNameText', lambda api: api._get_json('system/getNameText'),
             {'response_code': 0, 'input_list': [{'id': 'tv', 'text': 'Television'}]},
             [*({'response_code': 0, 'input_list': entries} for entries in
                (None, 'bad', [None], [{'id': 'tv'}], [{'id': 1, 'text': 'TV'}]))]),
        ]

    def test_malformed_contents_count_once_without_false_success_or_recovery(self):
        for key, read, good, malformed in self.cases():
            with self.subTest(request=key):
                api = self.api()
                opener = MagicMock()
                reply = opener.open.return_value.__enter__.return_value
                with patch.dict(R[BRAND + 'Api'].__init__.__globals__, DEVICE_HTTP_OPENER=opener):
                    reply.read.return_value = json.dumps(good).encode()
                    read(api)
                    cached = copy.deepcopy(api.polling_responses.snapshot(time.monotonic())[key]['response'])
                    for bad in malformed:
                        reply.read.return_value = json.dumps(bad).encode()
                        with self.subTest(payload=bad), self.assertRaises(R['DeviceResponseError']):
                            read(api)
                        self.assertEqual(api.polling_responses.snapshot(time.monotonic())[key]['response'], cached)
                    counters = api.polling_responses.counters()[key]
                    self.assertEqual(counters['successes'], 1)
                    self.assertEqual(counters['failures'], len(malformed))
                    self.assertEqual(counters['malformed_responses'], len(malformed))
                    self.assertEqual(counters['consecutive_failures'], len(malformed))
                    self.assertEqual(counters['recoveries'], 0)
                    reply.read.return_value = json.dumps(good).encode()
                    read(api)
                counters = api.polling_responses.counters()[key]
                self.assertEqual(counters['successes'], 2)
                self.assertEqual(counters['recoveries'], 1)
                self.assertEqual(counters['consecutive_failures'], 0)

    def test_unknown_response_fields_are_preserved(self):
        for key, read, good, _ in self.cases():
            with self.subTest(request=key):
                api = self.api()
                opener = MagicMock()
                payload = dict(good, future_device_field={'extra': 42})
                opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                with patch.dict(R[BRAND + 'Api'].__init__.__globals__, DEVICE_HTTP_OPENER=opener):
                    read(api)
                self.assertEqual(api.polling_responses.snapshot(time.monotonic())[key]['response'], payload)

    def test_valid_empty_inventories_still_count_as_empty_success(self):
        cases = [('appControl/getApplicationList', lambda api: api.get_apps(), {'result': [[]]}),
                 ('avContent/getCurrentExternalInputsStatus', lambda api: api.get_inputs(), {'result': [[]]})]
        if BRAND == 'Yamaha':
            cases = [('system/getFeatures', lambda api: api._get_features(),
                      {'response_code': 0, 'system': {'input_list': [], 'func_list': []}})]
        for key, read, payload in cases:
            with self.subTest(request=key):
                api = self.api()
                opener = MagicMock()
                opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(payload).encode()
                with patch.dict(R[BRAND + 'Api'].__init__.__globals__, DEVICE_HTTP_OPENER=opener):
                    read(api)
                counters = api.polling_responses.counters()[key]
                self.assertEqual(counters['successes'], 1)
                self.assertEqual(counters['empty_inventories'], 1)
                self.assertEqual(counters['failures'], 0)

    @unittest.skipIf(BRAND == 'Sony', 'Amplifier response variations')
    def test_standby_and_unadvertised_speakers_do_not_require_optional_fields(self):
        api = self.api()
        opener = MagicMock()
        reply = opener.open.return_value.__enter__.return_value
        with patch.dict(R[BRAND + 'Api'].__init__.__globals__, DEVICE_HTTP_OPENER=opener):
            reply.read.return_value = b'{"response_code":0,"power":"standby"}'
            self.assertEqual(api.get_status()['power'], 'standby')
            reply.read.return_value = b'{"response_code":0,"speaker_a":true,"speaker_b":"unsupported"}'
            self.assertEqual(api.get_speaker_state(('a',)), (True, False))


if __name__ == '__main__':
    unittest.main()
