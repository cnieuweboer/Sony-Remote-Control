import json
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from pathlib import Path
import runpy
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QCoreApplication, QEvent

R = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'prototypes' / 'TVAmplifierCECHandoverPrototype.pyw'), run_name='test_proto')
G = R['Window'].__init__.__globals__

class Simulation:
    def __init__(self, leak=False, fail=False, latency=.005):
        self.lock = threading.Lock()
        self.input = 'tv'
        self.amp_volume = 80
        self.tv_volume = 42
        self.leak = leak
        self.fail = fail
        self.latency = latency
        self.active = dict(tv=0, amplifier=0)
        self.maximum = dict(tv=0, amplifier=0)
        self.events = []
    def call(self, role, action, value=None):
        with self.lock:
            self.active[role] += 1
            self.maximum[role] = max(self.maximum[role], self.active[role])
            self.events.append((time.monotonic(), role, action, value, self.input))
        try:
            time.sleep(self.latency)
            with self.lock:
                if action == 'input':
                    self.input = value
                    return {'response_code': 0}
                if action == 'volume':
                    if self.fail:
                        raise TimeoutError('fake failure')
                    if self.leak:
                        self.amp_volume += 1
                        self.tv_volume += 1
                    else:
                        self.tv_volume = value
                    return {'result': [0]}
                if role == 'tv':
                    return dict(power='active', target='speaker', volume=self.tv_volume, minVolume=0, maxVolume=100, mute=False)
                return dict(power='on', input=self.input, volume=self.amp_volume, max_volume=154, mute=False)
        finally:
            with self.lock:
                self.active[role] -= 1
    def device(self, role):
        class Fake:
            def poll(inner):return self.call(role, 'poll')
            def set_input(inner,value):return self.call(role,'input',value)
            def set_tv_volume(inner,value):return self.call(role,'volume',value)
        return Fake()

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
    def wait(self, predicate, seconds=3):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():return
            time.sleep(.002)
        self.fail('Timeout waiting; ' + self.w.log.toPlainText())
    def window(self, simulation):
        timing = patch.dict(G, BURST_SECONDS=.25, OBSERVE_SECONDS=.12, SEND_SECONDS=.05, POLL_SECONDS=.03)
        timing.start(); self.addCleanup(timing.stop)
        self.w = R['Window']({'Host':'127.0.0.1','PreSharedKey':'dummy'}, {'Host':'127.0.0.1'})
        self.w.devices = {role:simulation.device(role) for role in ('tv','amplifier')}
        self.w.show()
        def cleanup():
            self.w.close()
            self.w.pool.waitForDone()
            self.app.processEvents()
            self.w.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.addCleanup(cleanup)
        self.wait(lambda:self.w.fresh('tv') and self.w.fresh('amplifier'))
    def test_no_leak(self):
        sim=Simulation()
        self.window(sim)
        self.w.start_test()
        self.wait(lambda:self.w.status.text().startswith('RESULT'))
        self.assertFalse(self.w.changes)
        self.assertGreater(self.w.sent,0)
        self.assertLessEqual(self.w.sent,5)
        self.assertEqual(sim.maximum,dict(tv=1,amplifier=1))
        sends=[e for e in sim.events if e[2]=='volume']
        self.assertTrue(all(e[4]=='usb_dac' for e in sends))
        usb_reads=[e[0] for e in sim.events if e[1]=='amplifier' and e[2]=='poll' and e[4]=='usb_dac']
        self.assertLess(min(usb_reads), sends[0][0])
        self.assertLess(sends[-1][0],self.w.burst_end)
        self.assertEqual(sim.tv_volume,10)
    def test_leak_detected(self):
        sim=Simulation(leak=True)
        self.window(sim);self.w.start_test()
        self.wait(lambda:self.w.status.text().startswith('RESULT'))
        self.assertTrue(self.w.changes)
        self.assertIn('AMPLIFIER VOLUME CHANGED',self.w.log.toPlainText())
    def test_stop_never_queues_more_volume(self):
        sim=Simulation(latency=.02)
        self.window(sim);self.w.start_test()
        self.wait(lambda:self.w.sent>0)
        self.w.stop_test()
        deadline=time.monotonic()+.3
        while time.monotonic()<deadline:self.app.processEvents();time.sleep(.002)
        self.assertEqual(len([e for e in sim.events if e[2]=='volume']),1)
    def test_failure_stops(self):
        sim=Simulation(fail=True)
        self.window(sim);self.w.start_test()
        self.wait(lambda:self.w.stage is None and 'failure' in self.w.status.text())
        self.assertEqual(self.w.sent,1)
    def settle(self, seconds=.2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)

    def test_external_input_change_automatically_starts_burst(self):
        sim = Simulation(leak=True)
        self.window(sim)
        with sim.lock:
            sim.input = 'usb_dac'
        self.wait(lambda:self.w.status.text().startswith('RESULT'))
        self.assertGreater(self.w.sent, 0)
        self.assertTrue(self.w.changes)
        self.assertFalse(any(event[2] == 'input' for event in sim.events))
        self.assertIn('AUTO detected tv', self.w.log.toPlainText())
        self.assertIn('REQUEST START', self.w.log.toPlainText())
        self.assertIn('after usb_dac detection', self.w.log.toPlainText())
        count = self.w.sent
        self.settle()
        self.assertEqual(self.w.sent, count)

    def test_initial_usb_dac_does_not_trigger_a_burst(self):
        sim = Simulation()
        sim.input = 'usb_dac'
        self.window(sim)
        self.settle()
        self.assertIsNone(self.w.stage)
        self.assertFalse(any(event[2] == 'volume' for event in sim.events))

    def test_automatic_watch_can_be_disabled(self):
        sim = Simulation()
        self.window(sim)
        self.w.auto_watch.setChecked(False)
        with sim.lock:
            sim.input = 'usb_dac'
        self.wait(lambda:self.w.state['amplifier']['input'] == 'usb_dac')
        self.settle()
        self.assertIsNone(self.w.stage)
        self.assertFalse(any(event[2] == 'volume' for event in sim.events))

    def test_stop_disarms_watch_before_late_input_confirmation(self):
        sim = Simulation()
        self.window(sim)
        self.w.start_test()
        self.w.stop_test()
        self.assertFalse(self.w.auto_watch.isChecked())
        with sim.lock:
            sim.input = 'usb_dac'
        self.settle()
        self.assertIsNone(self.w.stage)
        self.assertFalse(any(event[2] == 'volume' for event in sim.events))

    def test_config_and_exact_tv_method(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            a=root/'SonyRemote.json';b=root/'YamahaRemote.json'
            a.write_text(json.dumps(dict(Host='tv.local',PreSharedKey='SECRET',Ignored=True)))
            b.write_text(json.dumps(dict(Host='amp.local')))
            initial=[p.read_bytes() for p in (a,b)]
            tv,amp=R['load_settings'](root)
            self.assertEqual(initial,[p.read_bytes() for p in (a,b)])
            device=R['Device'](tv,'tv')
            opener=MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value=b'{"result":[0]}'
            with patch.dict(G,OPENER=opener):device.set_tv_volume(10)
            request=opener.open.call_args.args[0]
            data=json.loads(request.data)
            self.assertEqual(data['method'],'setAudioVolume')
            self.assertEqual(data['version'],'1.2')
            self.assertEqual(data['params'],[dict(target='speaker',volume='10',ui='on')])
            self.assertEqual(request.get_header('X-auth-psk'),'SECRET')

if __name__=='__main__':unittest.main()
