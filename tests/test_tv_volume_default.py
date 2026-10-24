"""Handover defaults use confirmed routing and never revive cancelled writes."""
import json
import tempfile
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QWidget

R = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'SonyRemote.pyw'), run_name='tv_defaults_test')


class TvDefaultMigrationTests(unittest.TestCase):
    def test_missing_block_and_partial_fields_are_saved_preserving_preferences(self):
        for settings in (None, {}, {'Volume': 12}, {'Volume': None, 'DelayMs': 750, 'RepeatCount': 2, 'RepeatIntervalMs': 200}):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'SonyRemote.json'
                document = R['_default_config_copy']()
                if settings is None:
                    del document['DefaultTvVolume']
                else:
                    document['DefaultTvVolume'] = settings
                document['Apps'] = {'example': {'Name': 'Example', 'NameOverride': 'Custom',
                                               'Hidden': True, 'Favorite': True, 'Removed': False}}
                path.write_text(json.dumps(document))
                with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path):
                    config = R['load_config']()
                saved = json.loads(path.read_text())
                self.assertEqual(saved['DefaultTvVolume'], config['DefaultTvVolume'])
                self.assertEqual(saved['Apps'], document['Apps'])
                self.assertEqual(saved['DefaultTvVolume'], dict(R['DEFAULT_CONFIG']['DefaultTvVolume'], **(settings or {})))

    def test_old_disabled_value_is_saved_as_null(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SonyRemote.json'
            document = R['_default_config_copy']()
            document['DefaultTvVolume'] = {'Volume': '', 'DelayMs': 750, 'RepeatCount': 2, 'RepeatIntervalMs': 200}
            path.write_text(json.dumps(document))
            with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path):
                config = R['load_config']()
            self.assertEqual(config['DefaultTvVolume'], {'Volume': None, 'DelayMs': 750, 'RepeatCount': 2, 'RepeatIntervalMs': 200})
            self.assertEqual(json.loads(path.read_text())['DefaultTvVolume'], config['DefaultTvVolume'])

    def test_old_duration_migrates_to_slot_count(self):
        for duration, count in ((0, 0), (200, 0), (300, 1), (600, 2), (1000, 4)):
            with self.subTest(duration=duration), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'SonyRemote.json'
                document = R['_default_config_copy']()
                document['DefaultTvVolume'] = {'Volume': 10, 'DelayMs': 750, 'RepeatMs': duration}
                path.write_text(json.dumps(document))
                with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path):
                    config = R['load_config']()
                expected = {'Volume': 10, 'DelayMs': 750, 'RepeatCount': count, 'RepeatIntervalMs': 200}
                self.assertEqual(config['DefaultTvVolume'], expected)
                self.assertEqual(json.loads(path.read_text())['DefaultTvVolume'], expected)

    def test_zero_volume_remains_enabled(self):
        config = R['validate_config']({'DefaultTvVolume': {'Volume': 0}}, allow_incomplete=True)
        self.assertEqual(config['DefaultTvVolume']['Volume'], 0)
        self.assertIsNotNone(config['DefaultTvVolume']['Volume'])

    def test_migration_does_not_overwrite_concurrent_edit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SonyRemote.json'
            path.write_text('{}')
            newer = '{"Host": "edited.local"}'
            save = R['save_config']
            def edit_then_save(config, **kwargs):
                path.write_text(newer)
                return save(config, **kwargs)
            with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path, save_config=edit_then_save):
                R['load_config']()
            self.assertEqual(path.read_text(), newer)

    def test_complete_configuration_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SonyRemote.json'
            text = json.dumps(R['_default_config_copy'](), indent=4)
            path.write_text(text)
            with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path):
                R['load_config']()
            self.assertEqual(path.read_text(), text)

    def test_invalid_block_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'SonyRemote.json'
            text = json.dumps({'DefaultTvVolume': {'DelayMs': -1}})
            path.write_text(text)
            with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path), self.assertRaises(R['ConfigError']):
                R['load_config']()
            self.assertEqual(path.read_text(), text)


class TvDefaultsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.w = QWidget()
        w = self.w
        w.config = {'DefaultTvVolume': {'Volume': 10, 'DelayMs': 1000, 'RepeatCount': 0, 'RepeatIntervalMs': 200}}
        w._shutting_down = False
        w._calibration_blocked = Mock(return_value=False)
        w.last_power = 'active'
        w._standby_confirmation_after = None
        w._tv_controls_available = lambda: R['SonyRemote']._tv_controls_available(w)
        w.amplifier_client = SimpleNamespace(state_is_fresh=True)
        w.amplifier_state = dict(online=True, power='on', valid_for_ms=6000, input='usb_dac')
        w._amplifier_tv_input = lambda: 'tv'
        w.api = SimpleNamespace(get_volume=Mock(return_value={'minVolume': 0, 'maxVolume': 100}), set_volume=Mock())
        w._request_poll = Mock()
        w.handle_request_error = Mock()
        self.jobs = []
        w.run_command = lambda fn, **callbacks: self.jobs.append((fn, callbacks)) or True
        self.c = R['TvVolumeDefault'](w)
        self.c.start()
        self.c.timer.stop()
        self.addCleanup(self.c.cancel)
        self.addCleanup(w.deleteLater)

    def execute(self):
        fn, callbacks = self.jobs.pop(0)
        try:
            fn()
        except Exception as error:
            callbacks['on_error'](error)
        callbacks['on_finished']()

    def test_delay_and_single_send(self):
        self.c.start()
        self.assertEqual(self.c.timer.interval(), 1000)
        self.assertFalse(self.jobs)
        self.c.timer.stop()
        self.c.send()
        self.execute()
        self.w.api.set_volume.assert_called_once_with(10)
        self.assertFalse(self.c.timer.isActive())
        self.w._request_poll.assert_called_once()

    def test_cancel_discards_already_queued_write(self):
        self.c.send()
        self.c.cancel()
        self.execute()
        self.w.api.get_volume.assert_not_called()
        self.w.api.set_volume.assert_not_called()

    def test_change_during_range_read_cancels_write(self):
        def read():
            self.c.cancel()
            return {'minVolume': 0, 'maxVolume': 100}
        self.w.api.get_volume.side_effect = read
        self.c.send()
        self.execute()
        self.w.api.set_volume.assert_not_called()

    def test_exact_repeat_count_despite_slow_work(self):
        self.w.config['DefaultTvVolume'].update(RepeatCount=2, RepeatIntervalMs=350)
        self.c.start()
        self.c.timer.stop()
        for index in range(3):
            self.c.send()
            self.assertFalse(self.c.timer.isActive())  # Never overlap requests.
            with patch.object(R['time'], 'monotonic', return_value=10000 * (index + 1)):
                self.execute()
            self.assertEqual(self.w.api.set_volume.call_count, index + 1)
            self.assertEqual(self.c.remaining, 2 - index)
            if index < 2:
                self.assertTrue(self.c.timer.isActive())
                self.assertEqual(self.c.timer.interval(), 350)
                self.c.timer.stop()
        self.assertFalse(self.c.timer.isActive())
        self.c.send()
        self.assertFalse(self.jobs)
        self.w._request_poll.assert_called_once()

    def test_repeat_failure_stops_remaining_sends(self):
        self.w.config['DefaultTvVolume'].update(RepeatCount=3)
        self.c.start()
        self.c.timer.stop()
        self.c.send()
        self.execute()
        self.c.timer.stop()
        self.w.api.set_volume.side_effect = TimeoutError('uncertain outcome')
        self.c.send()
        self.execute()
        self.assertEqual(self.w.api.set_volume.call_count, 2)
        self.assertEqual(self.c.remaining, 0)
        self.assertFalse(self.c.timer.isActive())

    def test_invalid_range_never_sends_or_retries(self):
        self.w.api.get_volume.return_value = {'minVolume': 20, 'maxVolume': 100}
        self.c.send()
        self.execute()
        self.w.api.set_volume.assert_not_called()
        self.w.handle_request_error.assert_called_once()
        self.assertFalse(self.c.timer.isActive())

    def test_disabled_and_unsafe_states_do_not_start(self):
        self.w.config['DefaultTvVolume']['Volume'] = None
        self.c.start()
        self.assertFalse(self.c.timer.isActive())
        self.w.config['DefaultTvVolume']['Volume'] = 10
        for change in ('standby', 'stale', 'input', 'calibration', 'shutdown'):
            with self.subTest(change=change):
                self.w.last_power = 'standby' if change == 'standby' else 'active'
                self.w.amplifier_client.state_is_fresh = change != 'stale'
                self.w.amplifier_state['input'] = 'tv' if change == 'input' else 'usb_dac'
                self.w._calibration_blocked.return_value = change == 'calibration'
                self.w._shutting_down = change == 'shutdown'
                self.c.start()
                self.assertFalse(self.c.timer.isActive())

    def test_config_validation(self):
        validate = R['validate_config']
        self.assertEqual(validate({}, allow_incomplete=True)['DefaultTvVolume'],
                         {'Volume': None, 'DelayMs': 1000, 'RepeatCount': 0, 'RepeatIntervalMs': 200})
        for value in ({'Volume': ''}, {'Volume': 10.5}, {'Volume': False}, {'Volume': True},
                      {'Volume': -1}, {'Volume': '10'}, {'DelayMs': -1},
                      {'RepeatCount': 1.5}, {'RepeatCount': -1}, {'RepeatCount': True},
                      {'RepeatIntervalMs': 0}, {'RepeatIntervalMs': False},
                      {'RepeatIntervalMs': None}, {'RepeatMs': 0}, {'Unknown': 1}, []):
            with self.subTest(value=value), self.assertRaises(R['ConfigError']):
                validate({'DefaultTvVolume': value}, allow_incomplete=True)

    def test_only_confirmed_tv_to_other_transition_starts(self):
        w = self.w
        w.tv_volume_default = self.c
        w._amplifier_error_category = ""
        w._amplifier_routed = True
        w.amplifier_state['input'] = 'tv'
        w._volume_generation = 0
        w._volume_route_epoch = 0
        w.volume_rate_timer = Mock()
        w.calibration_active = False
        w._refresh_playback_routing = Mock()
        w._update_volume_controls = Mock()
        w.config['VolumeControls'] = False
        w._use_amplifier = lambda: w.amplifier_state is not None and w.amplifier_state['input'] == 'tv'
        next_state = dict(w.amplifier_state, input='usb_dac')
        R['SonyRemote']._amplifier_state_changed(w, next_state)
        self.assertTrue(self.c.timer.isActive())
        self.c.cancel()
        R['SonyRemote']._amplifier_state_changed(w, next_state)
        self.assertFalse(self.c.timer.isActive())
        # Offline/disconnected fallback must not be mistaken for an input handover.
        w._amplifier_routed = True
        w.amplifier_state = dict(next_state, input='tv')
        R['SonyRemote']._amplifier_state_changed(w, None)
        self.assertFalse(self.c.timer.isActive())

    def test_absolute_api_payload(self):
        api = R['SonyApi']('example.test', 'fake')
        api.rpc = Mock()
        api.set_volume(12)
        api.rpc.assert_called_once_with('audio', 'setAudioVolume',
            [{'target': 'speaker', 'volume': '12', 'ui': 'on'}], version='1.2')


if __name__ == '__main__':
    unittest.main()
