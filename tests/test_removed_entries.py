"""Inventory absence confirmation, preference preservation, and guarded persistence."""
import copy
import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from PySide6.QtWidgets import QApplication, QWidget

ROOT = Path(__file__).resolve().parents[1]
BRAND = 'Sony' if (ROOT/'SonyRemote.pyw').exists() else 'Yamaha'
R = runpy.run_path(str(ROOT/(BRAND+'Remote.pyw')),run_name='removed_entry_tests')
G = R['DiscoveryTracker'].__init__.__globals__


def entry(**extra):
    return dict(Name='Old name',NameOverride='My name',Hidden=True,Favorite=True,Removed=False,**extra)


class TrackerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.now = 100.
        self.enabled = True
        self.parent = QWidget()
        self.refresh = Mock()
        self.clock = patch.dict(G,time=SimpleNamespace(monotonic=lambda:self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.tracker = R['DiscoveryTracker'](self.parent,('Inputs',),self.refresh,lambda collection:self.enabled)
        self.addCleanup(self.tracker.pause)
        self.addCleanup(self.parent.deleteLater)
        self.settings = {'old':entry(),'stay':entry()}

    def observe(self,present,allow=True):
        return self.tracker.observe('Inputs',set(present),self.settings,allow_missing=allow,observed_at=self.now)

    def test_two_successful_inventories_at_least_five_seconds_apart(self):
        before = copy.deepcopy(self.settings['old'])
        self.assertFalse(self.observe(['stay']))
        self.now += 4.999
        self.assertFalse(self.observe(['stay']))
        self.now += .002
        self.assertTrue(self.observe(['stay']))
        self.assertEqual(self.settings['old'],dict(before,Removed=True))
        self.assertEqual(list(self.settings),['old','stay'])

    def test_timer_requests_fresh_inventory_but_does_not_remove_by_itself(self):
        self.observe(['stay'])
        self.now += 5.01
        self.tracker.tick()
        self.refresh.assert_called_once_with('Inputs')
        self.assertFalse(self.settings['old']['Removed'])
        self.tracker.tick()
        self.assertEqual(self.refresh.call_count,1)
        self.now += 5.01
        self.tracker.tick()
        self.assertEqual(self.refresh.call_count,2)
        self.assertTrue(self.observe(['stay']))

    def test_verification_timer_requests_a_check_before_any_removal(self):
        from PySide6.QtTest import QTest
        with patch.dict(G,REMOVAL_CONFIRM_SECONDS=.03):
            self.observe(['stay'])
            self.now += .031
            QTest.qWait(50)
            self.refresh.assert_called_once_with('Inputs')
            self.assertFalse(self.settings['old']['Removed'])
            self.assertTrue(self.observe(['stay']))

    def test_return_before_confirmation_cancels_pending_removal(self):
        self.observe(['stay'])
        self.now += 6
        self.assertFalse(self.observe(['old','stay']))
        self.assertFalse(self.tracker.pending['Inputs'])
        self.now += 30
        self.assertFalse(self.observe(['stay']))
        self.assertFalse(self.settings['old']['Removed'])

    def test_return_after_removal_preserves_preferences_and_order(self):
        self.observe(['stay'])
        self.now += 6
        self.observe(['stay'])
        self.assertTrue(self.observe(['old','stay']))
        self.assertEqual(self.settings['old'],entry())
        self.assertEqual(list(self.settings),['old','stay'])

    def test_standby_unknown_power_and_pause_cannot_confirm_absence(self):
        self.observe(['stay'])
        self.now += 10
        self.assertFalse(self.observe([],allow=False))
        self.assertFalse(self.tracker.pending['Inputs'])
        self.assertFalse(self.observe(['stay']))
        self.tracker.pause()
        self.assertFalse(self.tracker.verification.isActive())
        self.now += 10
        self.tracker.tick()
        self.refresh.assert_not_called()
        self.tracker.resume()
        self.assertFalse(self.observe(['stay']))

    def test_regular_discovery_uses_existing_cadence_and_defers_when_blocked(self):
        self.observe(['old','stay'])
        self.now += 29
        self.tracker.tick()
        self.refresh.assert_not_called()
        self.enabled = False
        self.now += 2
        self.tracker.tick()
        self.refresh.assert_not_called()
        self.enabled = True
        self.tracker.tick()
        self.refresh.assert_called_once()

    def test_invalid_partial_and_duplicate_inventory_are_not_accepted(self):
        for data in (None,{},[{}],[{'id':'x','name':''}],[{'id':'x','name':'a'}]*2):
            with self.subTest(data=data),self.assertRaises(R['DeviceResponseError']):
                R['_inventory_ids'](data,'id','name')
        self.assertEqual(R['_inventory_ids']([{'id':'hdmi','name':'HDMI','connection':False}], 'id','name'),{'hdmi'})


class ConfigurationTests(unittest.TestCase):
    def test_removed_is_a_strict_boolean_with_false_default(self):
        raw = {'Inputs':{'one':{'Name':'One','Favorite':True}}}
        config = R['validate_config'](raw,allow_incomplete=True)
        self.assertFalse(config['Inputs']['one']['Removed'])
        for value in ('true',1,None):
            with self.subTest(value=value),self.assertRaises(R['ConfigError']):
                R['validate_config']({'Inputs':{'one':{'Name':'One','Removed':value}}},allow_incomplete=True)

    def test_existing_entries_gain_removed_on_disk_without_losing_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/(BRAND+'Remote.json')
            raw = R['_default_config_copy']()
            raw['Inputs'] = {'z':{'Name':'Z','Favorite':True},'a':{'Name':'A','NameOverride':'Mine'}}
            path.write_text(json.dumps(raw))
            with patch.dict(G,CONFIG_PATH=path):
                config = R['load_config']()
            saved = json.loads(path.read_text())
            self.assertEqual(list(saved['Inputs']),['z','a'])
            self.assertFalse(saved['Inputs']['z']['Removed'])
            self.assertTrue(saved['Inputs']['z']['Favorite'])
            self.assertEqual(saved['Inputs'],config['Inputs'])


class InventoryUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_graceful_failures as fixture
        fixture.DiscoveryTests.setUpClass()

    def setUp(self):
        import test_graceful_failures as fixture
        self.fixture = fixture.DiscoveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.start_and_settle()
        self.w = self.fixture.window
        self.g = type(self.w).__init__.__globals__
        self.now = self.g['time'].monotonic()
        self.clock = patch.dict(self.g,time=SimpleNamespace(monotonic=lambda:self.now))
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.w.polling.periodic.stop()
        self.w.last_power = 'active'
        self.w._status_observed_at = self.now
        self.save = patch.object(self.w,'_save_config_without_restart')
        self.save_mock = self.save.start()
        self.addCleanup(self.save.stop)
        self.addCleanup(self.w.discovery.pause)

    def check_collection(self,collection):
        feature = collection.lower()
        self.w._discovery_ready.discard(feature)
        old = dict(Name='Old',NameOverride='Mine',Hidden=False,Favorite=True,Removed=False)
        self.w.config[collection] = {'old':old,'stay':dict(old,Name='Stay',NameOverride='',Favorite=False)}
        self.w._build_favorites_from_config()
        self.w._fit_and_lock_window()
        before = self.w.height()
        entries = [{'uri':'stay','title':'Stay','connection':False,'status':'false'}]
        self.w._discovery_succeeded(feature,entries,self.now)
        combo = self.w.app_combo if collection=='Apps' else self.w.input_combo
        self.assertGreaterEqual(combo.findData('old'),0)
        self.assertFalse(old['Removed'])
        self.now += 5.1
        self.w._status_observed_at = self.now
        self.w._discovery_succeeded(feature,entries,self.now)
        self.assertTrue(old['Removed'])
        self.assertEqual(combo.findData('old'),-1)
        self.assertGreaterEqual(combo.findData('stay'),0)  # disconnected input is still present
        self.assertLess(self.w.height(),before)
        entries.append({'uri':'old','title':'Old'})
        self.w._discovery_succeeded(feature,entries,self.now)
        self.assertFalse(old['Removed'])
        self.assertEqual(combo.itemText(combo.findData('old')),'Mine')
        self.assertTrue(old['Favorite'])
        self.assertEqual(list(self.w.config[collection]),['old','stay'])
        self.assertEqual(self.w.height(),before)

    def test_apps_rebuild_and_resize_without_restart(self):
        self.check_collection('Apps')

    def test_inputs_rebuild_and_resize_without_restart(self):
        self.check_collection('Inputs')

    def test_malformed_list_does_not_confirm_a_pending_removal(self):
        old = dict(Name='Old',NameOverride='',Hidden=False,Favorite=True,Removed=False)
        self.w.config['Apps'] = {'old':old}
        self.w._discovery_succeeded('apps',[],self.now)
        self.now += 6
        self.w._status_observed_at = self.now
        self.w._discovery_succeeded('apps',[{'uri':'broken'}],self.now)
        self.assertFalse(old['Removed'])

    def test_periodic_input_response_is_reused_for_inventory(self):
        entries = [{'uri':'hdmi','title':'HDMI','connection':False}]
        self.w._discovery_ready.add('inputs')
        self.w._discovery_pending.clear()
        with patch.object(self.w.api,'get_inputs',return_value=entries) as get, \
             patch.object(self.w,'_discovery_succeeded') as observe:
            self.w._poll_input_status()
            self.fixture.wait(lambda:not self.w._input_status_running)
            get.assert_called_once()
            observe.assert_called_once()
            self.assertEqual(observe.call_args.args[:2],('inputs',entries))
