"""Missing-setting completion preserves explicit configuration and dynamic entries."""
import copy
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Sony' if (ROOT / 'SonyRemote.pyw').exists() else 'Yamaha'
R = runpy.run_path(str(ROOT / f'{NAME}Remote.pyw'), run_name='configuration_defaults_tests')


class ConfigurationDefaultsTests(unittest.TestCase):
    def test_nested_missing_only_and_independent_copies(self):
        defaults = {'Group': {'A': 5, 'B': [1]}, 'Enabled': True, 'Text': 'default'}
        config = {'Group': {'A': 0}, 'Enabled': False, 'Text': ''}
        self.assertTrue(R['_add_missing_settings'](config, defaults))
        self.assertEqual(config, {'Group': {'A': 0, 'B': [1]}, 'Enabled': False, 'Text': ''})
        config['Group']['B'].append(2)
        self.assertEqual(defaults['Group']['B'], [1])
        self.assertFalse(R['_add_missing_settings'](config, defaults))

    def test_dynamic_entries_are_not_inspected(self):
        config = {'Apps': {'app': {}}, 'Inputs': {'tv': {}}}
        before = copy.deepcopy(config)
        R['_add_missing_settings'](config, {'Apps': {'unexpected': {}}, 'Inputs': {'unexpected': {}}})
        self.assertEqual(config, before)

    def test_explicit_list_and_wrong_types_are_not_replaced(self):
        config = {'Group': None, 'List': [], 'Other': 'kept'}
        before = copy.deepcopy(config)
        R['_add_missing_settings'](config, {'Group': {'A': 1}, 'List': [1]})
        self.assertEqual(config, before)

    def test_load_persists_all_missing_settings_preserving_user_values(self):
        raw = {'Host': 'device.local', 'DarkMode': False, 'Shortcuts': {'Mute': []}}
        if NAME == 'Sony':
            raw.update(VolumeRateLimit={'Active': False}, DefaultTvVolume={'Volume': ''})
        else:
            raw['GlobalShortcuts'] = {'VolumeUp': []}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'remote.json'
            path.write_text(json.dumps(raw))
            with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path):
                R['load_config']()
                saved = json.loads(path.read_text())
                text = path.read_text()
                R['load_config']()
                self.assertEqual(path.read_text(), text)
            self.assertEqual(set(saved), set(R['DEFAULT_CONFIG']))
            self.assertFalse(saved['DarkMode'])
            self.assertEqual(saved['Shortcuts']['Mute'], [])
            self.assertEqual(saved['Inputs'], {})
            if NAME == 'Sony':
                self.assertEqual(saved['VolumeRateLimit'], {'Active': False, 'RateMs': 500})
                self.assertEqual(saved['DefaultTvVolume']['Volume'], '')
            else:
                self.assertEqual(saved['GlobalShortcuts']['VolumeUp'], [])

    def test_invalid_existing_values_do_not_get_repaired_or_saved(self):
        for raw in ({'DarkMode': None}, {'Shortcuts': None}, {'Unknown': True}):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'remote.json'
                text = json.dumps(raw)
                path.write_text(text)
                with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path), self.assertRaises(R['ConfigError']):
                    R['load_config']()
                self.assertEqual(path.read_text(), text)

    def test_concurrent_edit_prevents_migration_save(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'remote.json'
            path.write_text('{}')
            newer = '{"Host": "edited.local"}'
            save = R['save_config']
            def edit_then_save(config, **kwargs):
                path.write_text(newer)
                return save(config, **kwargs)
            with patch.dict(R['load_config'].__globals__, CONFIG_PATH=path, save_config=edit_then_save):
                R['load_config']()
            self.assertEqual(path.read_text(), newer)


if __name__ == '__main__':
    unittest.main()
