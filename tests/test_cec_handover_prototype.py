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
            def power(inner):return self.call(role, 'power')['power']
            def set_input(inner,value):return self.call(role,'input',value)
            def set_tv_volume(inner,value):return self.call(role,'volume',value)
        return Fake()

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def wait(self, predicate, seconds=4):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(.002)
        self.fail('Timeout waiting; ' + self.w.log.toPlainText())

    def settle(self, seconds=.2):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.002)

    def window(self, simulation, tv=True, amp=True, duration=250, settle=0):
        timing = patch.dict(G, SEND_SECONDS=.05, POLL_SECONDS=.03)
        timing.start()
        self.addCleanup(timing.stop)
        self.w = R['Window']({'Host':'127.0.0.1','PreSharedKey':'dummy'}, {'Host':'127.0.0.1'})
        self.w.devices = {role:simulation.device(role) for role in ('tv','amplifier')}
        self.w.duration.setValue(duration)
        if settle is not None:
            self.w.settle_delay.setValue(settle)
        self.w.measure_tv.setChecked(tv)
        self.w.measure_amp.setChecked(amp)
        self.submissions = []
        original = self.w.submit
        def submit(role, kind, function, allowed=None):
            accepted = original(role, kind, function, allowed)
            if accepted:
                self.submissions.append((time.monotonic(), role, kind))
            return accepted
        self.w.submit = submit
        self.w.show()
        window = self.w
        def cleanup():
            window.close()
            window.pool.waitForDone()
            self.app.processEvents()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self.addCleanup(cleanup)
        self.wait(lambda:self.w.tv_ready() and self.w.fresh('amplifier'))

    def run_button(self):
        self.w.start_test()
        self.wait(lambda:self.w.status.text().startswith('RESULT'))

    def test_no_leak_and_full_measurement_rounds(self):
        sim = Simulation()
        self.window(sim)
        self.run_button()
        self.assertFalse(self.w.changes)
        self.assertGreater(self.w.sent, 0)
        self.assertLessEqual(self.w.sent, 5)
        self.assertEqual(sim.maximum, dict(tv=1, amplifier=1))
        sends = [event for event in sim.events if event[2] == 'volume']
        self.assertTrue(all(event[4] == 'usb_dac' for event in sends))
        self.assertLess(sends[-1][0], self.w.repeat_end)
        kinds = [kind for _, role, kind in self.submissions if role == 'tv']
        self.assertEqual(kinds.count('measure pre'), 1)
        self.assertEqual(kinds.count('measure post'), self.w.sent)
        self.assertEqual(kinds.count('measure final'), 1)
        self.assertEqual(sim.tv_volume, 10)
        self.assertIn('pre measurement REUSED', self.w.log.toPlainText())

    def test_leak_detected_with_only_required_amplifier_monitoring(self):
        sim = Simulation(leak=True)
        self.window(sim, tv=False, amp=False)
        self.run_button()
        self.assertTrue(self.w.changes)
        self.assertIn('TV volume effect unmeasured', self.w.status.text())
        self.assertFalse(any(kind.startswith('measure') for _, _, kind in self.submissions))
        tv_calls = [kind for started, role, kind in self.submissions
                    if role == 'tv' and self.w.detected_at <= started <= self.w.last_send_finished]
        self.assertTrue(all(kind == 'volume' for kind in tv_calls), tv_calls)
        total = len([event for event in sim.events if event[1] == 'tv'])
        self.settle(.15)
        self.assertEqual(len([event for event in sim.events if event[1] == 'tv']), total)

    def test_zero_duration_sends_once_then_final_round(self):
        sim = Simulation()
        self.window(sim, duration=0)
        self.run_button()
        self.assertEqual(self.w.sent, 1)
        self.assertEqual(self.w.skipped, 0)
        self.assertEqual(len([e for e in sim.events if e[2] == 'volume']), 1)
        self.assertEqual(len([s for s in self.submissions if s[1:] == ('tv','measure final')]), 1)

    def test_measurement_toggles_are_independent(self):
        for tv, amp in ((True, False), (False, True)):
            sim = Simulation()
            self.window(sim, tv=tv, amp=amp, duration=0)
            self.run_button()
            reads = [(role, kind) for _, role, kind in self.submissions if kind.startswith('measure')]
            self.assertEqual(any(role == 'tv' for role, _ in reads), tv)
            self.assertEqual(any(role == 'amplifier' for role, _ in reads), amp)
            # Each iteration closes its window before creating the next fixture.
            self.w.close()
            self.w.pool.waitForDone()
            self.app.processEvents()

    def test_slow_reads_skip_slots_without_backlog(self):
        sim = Simulation(latency=.08)
        self.window(sim, duration=250)
        self.run_button()
        self.assertGreater(self.w.skipped, 0)
        self.assertLess(self.w.sent, 5)
        self.assertIn('SKIPPED', self.w.log.toPlainText())
        self.assertEqual(sim.maximum, dict(tv=1, amplifier=1))
        self.assertTrue(all(e[0] < self.w.repeat_end for e in sim.events if e[2] == 'volume'))

    def test_final_delay_is_separate_from_repeat_window(self):
        sim = Simulation()
        self.window(sim, duration=0)
        self.w.final_delay.setValue(100)
        self.run_button()
        final = next(started for started, role, kind in self.submissions if role == 'tv' and kind == 'measure final')
        self.assertGreaterEqual(final - self.w.last_send_finished, .095)
        self.assertEqual(self.w.sent, 1)

    def test_stop_disarms_and_never_queues_more_volume(self):
        sim = Simulation(latency=.02)
        self.window(sim)
        self.w.start_test()
        self.wait(lambda:self.w.sent > 0)
        self.w.stop_test()
        self.settle(.3)
        self.assertFalse(self.w.auto_watch.isChecked())
        self.assertEqual(len([e for e in sim.events if e[2] == 'volume']), 1)

    def test_failure_stops_without_retry(self):
        sim = Simulation(fail=True)
        self.window(sim)
        self.w.start_test()
        self.wait(lambda:self.w.stage is None and 'failure' in self.w.status.text())
        self.assertEqual(self.w.sent, 1)

    def test_external_input_change_starts_automatically(self):
        sim = Simulation(leak=True)
        self.window(sim)
        with sim.lock:
            sim.input = 'usb_dac'
        self.wait(lambda:self.w.status.text().startswith('RESULT'))
        self.assertGreater(self.w.sent, 0)
        self.assertTrue(self.w.changes)
        self.assertFalse(any(e[2] == 'input' for e in sim.events))
        self.assertIn('AUTO tv', self.w.log.toPlainText())
        count = self.w.sent
        self.settle()
        self.assertEqual(self.w.sent, count)

    def test_initial_usb_dac_and_disabled_watch_do_not_send(self):
        sim = Simulation()
        sim.input = 'usb_dac'
        self.window(sim)
        self.settle()
        self.assertIsNone(self.w.stage)
        self.assertFalse(any(e[2] == 'volume' for e in sim.events))
        self.w.auto_watch.setChecked(False)
        with sim.lock:
            sim.input = 'tv'
        self.wait(lambda:self.w.state['amplifier']['input'] == 'tv')
        with sim.lock:
            sim.input = 'usb_dac'
        self.settle()
        self.assertFalse(any(e[2] == 'volume' for e in sim.events))

    def test_cancelled_worker_does_not_send(self):
        called = MagicMock()
        job = R['Job']('tv', 'volume', called, allowed=lambda started:False)
        job.run()
        called.assert_not_called()

    def test_settle_control_defaults_to_half_second(self):
        sim = Simulation()
        self.window(sim, settle=None)
        self.assertEqual(self.w.settle_delay.value(), 500)

    def test_settling_precedes_measurements_and_preserves_full_repeat_window(self):
        sim = Simulation()
        self.window(sim, settle=120, duration=250)
        self.run_button()
        self.assertGreaterEqual(self.w.first_send_at - self.w.detected_at, .12)
        self.assertAlmostEqual(self.w.repeat_end - self.w.first_send_at, .25, places=4)
        pre = [started for started, role, kind in self.submissions if kind == 'measure pre']
        self.assertTrue(pre)
        self.assertTrue(all(started >= self.w.settle_due for started in pre))
        monitors = [started for started, role, kind in self.submissions
                    if kind == 'monitor' and self.w.detected_at < started < self.w.settle_due]
        self.assertTrue(monitors)
        self.assertIn('Settling delay COMPLETE', self.w.log.toPlainText())

    def test_stop_during_settling_never_sends(self):
        sim = Simulation()
        self.window(sim, settle=200)
        self.w.start_test()
        self.wait(lambda:self.w.stage == 'settle')
        self.w.stop_test()
        self.settle(.3)
        self.assertFalse(any(event[2] == 'volume' for event in sim.events))
        self.assertFalse(any(kind.startswith('measure') for _, _, kind in self.submissions))

    def test_input_change_during_settling_cancels(self):
        sim = Simulation()
        self.window(sim, settle=200)
        self.w.start_test()
        self.wait(lambda:self.w.stage == 'settle')
        with sim.lock:
            sim.input = 'tv'
        self.wait(lambda:self.w.stage is None)
        self.assertFalse(any(event[2] == 'volume' for event in sim.events))

    def test_failure_during_settling_cancels(self):
        sim = Simulation()
        self.window(sim, settle=200)
        self.w.start_test()
        self.wait(lambda:self.w.stage == 'settle')
        self.w.devices['amplifier'].poll = MagicMock(side_effect=TimeoutError('fake timeout'))
        self.wait(lambda:self.w.stage is None)
        self.assertFalse(any(event[2] == 'volume' for event in sim.events))

    def test_config_and_exact_tv_method_with_http_timing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            a, b = root / 'SonyRemote.json', root / 'YamahaRemote.json'
            a.write_text(json.dumps(dict(Host='tv.local', PreSharedKey='SECRET', Ignored=True)))
            b.write_text(json.dumps(dict(Host='amp.local')))
            initial = [p.read_bytes() for p in (a,b)]
            tv, amp = R['load_settings'](root)
            self.assertEqual(initial, [p.read_bytes() for p in (a,b)])
            trace = MagicMock()
            device = R['Device'](tv, 'tv', trace)
            opener = MagicMock()
            opener.open.return_value.__enter__.return_value.read.return_value = b'{"result":[0]}'
            with patch.dict(G, OPENER=opener):
                device.set_tv_volume(10)
            request = opener.open.call_args.args[0]
            data = json.loads(request.data)
            self.assertEqual(data['method'], 'setAudioVolume')
            self.assertEqual(data['version'], '1.2')
            self.assertEqual(data['params'], [dict(target='speaker', volume='10', ui='on')])
            self.assertEqual(trace.call_args.args[:2], ('tv', 'audio/setAudioVolume'))
            self.assertTrue(trace.call_args.args[-1])
            self.assertNotIn('SECRET', repr(trace.call_args))

if __name__ == '__main__':
    unittest.main()
