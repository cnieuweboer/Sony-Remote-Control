"""Direct Sony/Yamaha CEC handover experiment. Requires only PySide6.

Place SonyRemote.json and YamahaRemote.json beside this script. No IPC, imports
from remotes, setting writes, automatic mute changes or automatic restoration.
"""
import ipaddress
import math
import threading
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget)

__version__ = '2026.10.24.4'
TV_INPUT = 'tv'
OTHER_INPUT = 'usb_dac'
POLL_SECONDS = .25
SEND_SECONDS = .20
MAX_REPEAT_MS = 30000
MAX_FINAL_DELAY_MS = 10000
SKIPPED = object()
TIMEOUT_SECONDS = 2.0
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_settings(folder):
    def read(filename):
        value = json.loads((folder / filename).read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError(filename + ' must contain a JSON object.')
        return value
    tv, amp = read('SonyRemote.json'), read('YamahaRemote.json')
    for name, config in (('SonyRemote.json', tv), ('YamahaRemote.json', amp)):
        if not isinstance(config.get('Host'), str) or not config['Host'].strip():
            raise ValueError(name + ' needs a non-empty Host.')
    if not isinstance(tv.get('PreSharedKey'), str) or not tv['PreSharedKey'].strip():
        raise ValueError('SonyRemote.json needs a non-empty PreSharedKey.')
    return tv, amp


def base_url(host):
    host = host.strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if any(char in host for char in '/:@?#') or any(char.isspace() for char in host):
            raise ValueError('Host must be a hostname or IP address, without a URL or port.')
    else:
        if address.version == 6:
            host = '[' + host.replace('%', '%25') + ']'
    return 'http://' + host


class Device:
    def __init__(self, settings, role, trace=None):
        self.role = role
        self.trace = trace
        self.url = base_url(settings['Host'])
        self.psk = settings.get('PreSharedKey', '')
        self.request_id = 0

    def read_json(self, request, endpoint):
        started = time.monotonic()
        ok = False
        try:
            with OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
                result = json.loads(response.read().decode('utf-8'))
            if not isinstance(result, dict):
                raise ValueError('Device did not return a JSON object.')
            ok = True
            return result
        finally:
            if self.trace:
                self.trace(self.role, endpoint, started, time.monotonic(), ok)

    def tv_rpc(self, service, method, params=None, version='1.0'):
        self.request_id += 1
        payload = dict(method=method, id=self.request_id,
                       params=[] if params is None else params, version=version)
        request = urllib.request.Request(self.url + '/sony/' + service,
            data=json.dumps(payload).encode(), method='POST',
            headers={'Content-Type': 'application/json', 'X-Auth-PSK': self.psk})
        result = self.read_json(request, service + "/" + method)
        if 'error' in result:
            raise RuntimeError('TV rejected request: ' + repr(result['error']))
        if not isinstance(result.get('result'), list):
            raise ValueError('TV response has no result list.')
        return result

    def amp_get(self, path):
        result = self.read_json(self.url + '/YamahaExtendedControl/v1/' + path, path.split('?')[0])
        if type(result.get('response_code')) is not int or result['response_code'] != 0:
            raise RuntimeError('Amplifier rejected request (response_code=' + repr(result.get('response_code')) + ').')
        return result

    def poll(self):
        if self.role == 'amplifier':
            data = self.amp_get('main/getStatus')
            return {key: data.get(key) for key in ('power', 'input', 'volume', 'max_volume', 'mute')}
        power = self.power()
        if power != 'active':
            return dict(power=power, volume=None, mute=None)
        outputs = self.tv_rpc('audio', 'getVolumeInformation')['result'][0]
        info = next((item for item in outputs if item.get('target') == 'speaker'), None)
        if info is None:
            raise ValueError('TV did not report its speaker volume target.')
        return dict(power=power, **{key: info.get(key) for key in ('target', 'volume', 'mute', 'minVolume', 'maxVolume')})

    def power(self):
        return self.tv_rpc('system', 'getPowerStatus')['result'][0]['status']

    def set_input(self, input_id):
        return self.amp_get('main/setInput?input=' + quote(input_id, safe=''))

    def set_tv_volume(self, volume):
        # Exact absolute-volume method/version from the user's verified prototype.
        return self.tv_rpc('audio', 'setAudioVolume',
            [{'target': 'speaker', 'volume': str(volume), 'ui': 'on'}], '1.2')


class Signals(QObject):
    started = Signal(str, str, float)
    done = Signal(str, str, object, object, float, float)


class Job(QRunnable):
    def __init__(self, role, kind, function, allowed=None):
        super().__init__()
        self.role, self.kind, self.function = role, kind, function
        self.allowed = allowed
        self.signals = Signals()

    def run(self):
        started = time.monotonic()
        if self.allowed is not None and not self.allowed(started):
            self.signals.done.emit(self.role, self.kind, SKIPPED, None, started, started)
            return
        self.signals.started.emit(self.role, self.kind, started)
        result = error = None
        try:
            result = self.function()
        except Exception as caught:
            error = caught
        self.signals.done.emit(self.role, self.kind, result, error, started, time.monotonic())


class Window(QMainWindow):
    http_event = Signal(str, str, float, float, bool)

    def __init__(self, tv, amp):
        super().__init__()
        self.devices = dict(tv=Device(tv, 'tv', self.http_event.emit),
                            amplifier=Device(amp, 'amplifier', self.http_event.emit))
        self.http_event.connect(self.log_http)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(2)
        self.jobs = {}
        self.state = dict(tv=None, amplifier=None)
        self.observed = dict(tv=0., amplifier=0.)
        self.sample_started = dict(tv=0., amplifier=0.)
        self.audio_observed = dict(tv=0., amplifier=0.)
        self.audio_started = dict(tv=0., amplifier=0.)
        self.next_poll = dict(tv=0., amplifier=0.)
        self.stage = None
        self.tv_initialized = False
        self.pending_reads = {}
        self.cancelled = threading.Event()
        self.manual_input = None
        self.closed = False
        self.started = time.monotonic()
        self.sent = self.skipped = 0
        self.changes = []
        self.first_send_at = self.repeat_end = self.next_slot = None
        self.baseline = None
        self.setWindowTitle('TV / amplifier CEC handover prototype ' + __version__)
        self.resize(960, 670)
        panel = QWidget(self)
        self.setCentralWidget(panel)
        layout = QVBoxLayout(panel)
        warning = QLabel('Close BOTH normal remotes. Use a silent TV source and safe amplifier volume. '
            'The TV and amplifier must stay on. No automatic mute or restoration: check their volume/mute/input afterward.')
        warning.setWordWrap(True)
        layout.addWidget(warning)
        info = QLabel('Required: amplifier input/status monitoring every 250 ms (also returns volume/mute). '
            'TV readiness/range is read initially or with Refresh; no recurring TV reads outside experiments. '
            'Optional measurements may slow sends; missed 200 ms opportunities are skipped.')
        info.setWordWrap(True)
        layout.addWidget(info)
        self.labels = {role: QLabel() for role in self.devices}
        for label in self.labels.values():
            label.setWordWrap(True)
            layout.addWidget(label)
        self.auto_watch = QCheckBox('Automatically test each confirmed tv → usb_dac transition')
        self.auto_watch.setChecked(True)
        layout.addWidget(self.auto_watch)
        row = QHBoxLayout()
        self.input_buttons = []
        for input_id in (TV_INPUT, OTHER_INPUT):
            button = QPushButton('Amplifier input: ' + input_id)
            button.clicked.connect(lambda checked=False, value=input_id: self.switch(value))
            self.input_buttons.append(button)
            row.addWidget(button)
        self.refresh = QPushButton('Refresh TV readiness / range')
        self.refresh.clicked.connect(self.refresh_tv)
        row.addWidget(self.refresh)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel('TV target:'))
        self.target = QSpinBox()
        self.target.setRange(0, 100)
        self.target.setValue(10)
        row.addWidget(self.target)
        row.addWidget(QLabel('Delay after input confirmation (ms):'))
        self.settle_delay = QSpinBox()
        self.settle_delay.setRange(0, 10000)
        self.settle_delay.setSingleStep(100)
        self.settle_delay.setValue(500)
        self.settle_delay.setToolTip('Wait after confirmed usb_dac before pre-measurements or volume commands. Required amplifier monitoring continues.')
        row.addWidget(self.settle_delay)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel('Repeat duration (ms):'))
        self.duration = QSpinBox()
        self.duration.setRange(0, MAX_REPEAT_MS)
        self.duration.setSingleStep(200)
        self.duration.setValue(2000)
        self.duration.setToolTip('0 sends once. Otherwise sends repeat every 200 ms within this elapsed window, from the first request start.')
        row.addWidget(self.duration)
        row.addWidget(QLabel('Final measurement delay (ms):'))
        self.final_delay = QSpinBox()
        self.final_delay.setRange(0, MAX_FINAL_DELAY_MS)
        self.final_delay.setSingleStep(100)
        self.final_delay.setValue(0)
        row.addWidget(self.final_delay)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.measure_tv = QCheckBox('Measure TV before / after commands and at end')
        self.measure_tv.setChecked(True)
        self.measure_tv.setToolTip('Each TV measurement reads power, then volume/mute (two HTTP requests). Off means no TV reads during the experiment.')
        self.measure_amp = QCheckBox('Extra amplifier measurements before / after / at end')
        self.measure_amp.setChecked(True)
        self.measure_amp.setToolTip('Reuse required status reads when suitable; otherwise request extra status. Required input monitoring always continues.')
        row.addWidget(self.measure_tv)
        row.addWidget(self.measure_amp)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.test = QPushButton('Switch to usb_dac + run test')
        self.test.clicked.connect(self.start_test)
        row.addWidget(self.test)
        self.stop = QPushButton('Stop / disarm automatic test')
        self.stop.clicked.connect(self.stop_test)
        row.addWidget(self.stop)
        save = QPushButton('Save log…')
        save.clicked.connect(self.save_log)
        row.addWidget(save)
        layout.addLayout(row)
        self.status = QLabel('Waiting for initial device status.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        self.timer = QTimer(self)
        self.timer.setInterval(10)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.message('INITIAL: one TV readiness/range read; required amplifier main/getStatus monitoring. '
                     'No output selection can be inferred from target="speaker" alone.')
        self.update_controls()

    def message(self, text):
        self.log.appendPlainText(f'{time.monotonic() - self.started:9.3f}s  {text}')

    def log_http(self, role, endpoint, started, ended, ok):
        if self.closed:
            return
        job = self.jobs.get(role)
        kind = job.kind if job else 'request'
        self.message(f'HTTP {role} [{kind}] {endpoint}: start +{started-self.started:.3f}s, '
                     f'end +{ended-self.started:.3f}s, {1000*(ended-started):.1f} ms; '
                     + ('JSON received' if ok else 'FAILED'))

    def fresh(self, role):
        limit = 1.5 if role == 'amplifier' else 3.0
        return self.state[role] is not None and time.monotonic() - self.observed[role] < limit

    def tv_ready(self):
        state = self.state['tv'] or {}
        minimum, maximum = state.get('minVolume'), state.get('maxVolume')
        return (state.get('power') == 'active'
                and type(minimum) is int and type(maximum) is int
                and minimum <= self.target.value() <= maximum)

    def submit(self, role, kind, function, allowed=None):
        if self.closed or role in self.jobs:
            return False
        job = Job(role, kind, function, allowed)
        self.jobs[role] = job
        job.signals.started.connect(self.request_started)
        job.signals.done.connect(self.completed)
        self.pool.start(job)
        return True

    def request_started(self, role, kind, started):
        if self.closed:
            return
        self.message(f'JOB {role} [{kind}] START +{started-self.started:.3f}s')
        if kind == 'volume' and self.stage:
            self.sent += 1
            if self.first_send_at is None:
                self.first_send_at = started
                self.repeat_end = started + self.options['duration_ms'] / 1000
                self.next_slot = started + SEND_SECONDS
                self.message(f'First volume request: +{started-self.detected_at:.3f}s after detection; '
                             f'repeat window ends +{self.repeat_end-self.started:.3f}s.')
            else:
                self.message(f'Volume request #{self.sent}: +{started-self.first_send_at:.3f}s from first send.')

    def completed(self, role, kind, result, error, request_started, ended):
        self.jobs.pop(role, None)
        if self.closed:
            return
        if result is SKIPPED:
            self.message(f'{role} [{kind}] skipped before starting: cancelled or repeat window ended.')
            if self.stage:
                self.stage = 'between'
            return
        if error is not None:
            self.message(f'{role} [{kind}] FAILED: {type(error).__name__}')
            self.state[role] = None
            self.next_poll[role] = time.monotonic() + .5
            if self.stage:
                self.finish('Test stopped on a request failure; no retry.')
            self.update_controls()
            return
        self.message(f'JOB {role} [{kind}] END +{ended-self.started:.3f}s ({1000*(ended-request_started):.1f} ms)')
        if kind in ('monitor', 'initial', 'refresh') or kind.startswith('measure '):
            self.accept_status(role, kind, result, request_started, ended)
        elif kind == 'volume' and self.stage:
            self.last_send_finished = ended
            self.message(f'TV volume {self.options["target"]} acknowledged; acknowledgement alone does not prove its effect.')
            self.stage = 'post'
            self.queue_measurements('post', ended)
        elif kind == 'test switch' and self.stage == 'switching':
            self.stage = 'wait_input'
            self.switch_completed = ended
            self.next_poll['amplifier'] = 0
        elif kind.startswith('input '):
            self.next_poll['amplifier'] = 0
        self.update_controls()

    def accept_status(self, role, kind, result, request_started, ended):
        previous = self.state[role] if self.fresh(role) else None
        previous_started = self.sample_started[role]
        self.state[role] = result
        self.audio_observed[role] = ended
        self.audio_started[role] = request_started
        if role == 'tv':
            self.tv_initialized = True
            if kind in ('initial', 'refresh'):
                minimum, maximum = result.get('minVolume'), result.get('maxVolume')
                if type(minimum) is int and type(maximum) is int and 0 <= minimum <= maximum <= 2147483647:
                    self.target.setRange(minimum, maximum)
        self.observed[role] = ended
        self.sample_started[role] = request_started
        self.next_poll[role] = ended + POLL_SECONDS
        self.message(f'{role} [{kind}] RESULT: ' + json.dumps(result))
        current = self.state[role]
        pending = self.pending_reads.get(role)
        if pending and request_started >= pending['after']:
            self.message(f'{role} {pending["phase"]} measurement satisfied by [{kind}]: ' + json.dumps(current))
            del self.pending_reads[role]
        expected_power = 'on' if role == 'amplifier' else 'active'
        if self.stage and current.get('power') != expected_power:
            self.finish(f'Test stopped: {role} is not on.')
            return
        if role != 'amplifier':
            return
        if self.stage in ('settle', 'pre', 'sending', 'post', 'between', 'final_delay', 'final'):
            if current.get('input') != OTHER_INPUT:
                self.finish('Test stopped: amplifier left usb_dac.')
                return
            if (type(current.get('volume')) is int and type(self.baseline.get('volume')) is int
                    and current['volume'] != self.baseline['volume']):
                if not self.changes or self.changes[-1] != current.get('volume'):
                    self.changes.append(current.get('volume'))
                    self.message('AMPLIFIER VOLUME CHANGED [' + self.stage + ']: ' + repr(current.get('volume')))
        elif self.stage == 'wait_input' and request_started >= self.switch_completed and current.get('input') == OTHER_INPUT:
            self.begin_experiment(current, 'Button-driven handover')
        elif (self.stage is None and self.auto_watch.isChecked() and previous
              and previous.get('power') == 'on' and previous.get('input') == TV_INPUT
              and current.get('power') == 'on' and current.get('input') == OTHER_INPUT):
            self.message(f'AUTO tv → usb_dac: previous TV-input poll start +{previous_started-self.started:.3f}s; '
                         f'USB report received +{ended-self.started:.3f}s. Exact physical switch time is unknown.')
            if self.tv_ready():
                self.begin_experiment(current, 'Automatic handover', previous)
            else:
                self.message('Automatic test skipped: refresh TV readiness/range first.')

    def begin_experiment(self, amp_status, reason, previous=None):
        if not self.tv_ready():
            self.finish('Test not started: TV readiness/range unavailable.')
            return
        self.options = dict(target=self.target.value(), duration_ms=self.duration.value(),
                            settle_delay_ms=self.settle_delay.value(),
                            final_delay_ms=self.final_delay.value(), tv=self.measure_tv.isChecked(),
                            amplifier=self.measure_amp.isChecked())
        self.cancelled = threading.Event()
        self.detected_at = time.monotonic()
        self.baseline = amp_status.copy()
        self.initial_tv_volume = None
        self.sent = self.skipped = 0
        self.changes = []
        self.first_send_at = self.repeat_end = self.next_slot = None
        self.pending_reads.clear()
        self.manual_input = None
        self.stage = 'settle'
        self.settle_due = self.detected_at + self.options['settle_delay_ms'] / 1000
        self.message(reason + ': SETTINGS ' + json.dumps(self.options))
        self.message('Confirmed usb_dac; amplifier baseline from REQUIRED input monitoring: ' + json.dumps(self.baseline))
        if previous:
            self.message('Previous tv-input sample: ' + json.dumps(previous))
        self.message(f'Settling delay START: {self.options["settle_delay_ms"]} ms after confirmation; '
                     f'due +{self.settle_due-self.started:.3f}s. Required input monitoring continues; no pre-measurements or volume sends yet.')
        self.update_controls()
        self.drive_experiment(time.monotonic())

    def queue_measurements(self, phase, after):
        self.pending_reads = {role: dict(phase=phase, after=after)
                              for role in ('tv', 'amplifier') if self.options[role]}

    def drive_reads(self):
        for role, pending in list(self.pending_reads.items()):
            if (self.sample_started[role] >= pending['after'] and self.state[role] is not None
                    and (role == 'amplifier' or self.audio_started[role] >= pending['after'])):
                self.message(f'{role} {pending["phase"]} measurement REUSED existing response: ' + json.dumps(self.state[role]))
                if pending['phase'] == 'pre' and role == 'tv':
                    self.initial_tv_volume = self.state[role].get('volume')
                del self.pending_reads[role]
            elif role not in self.jobs:
                self.submit(role, 'measure ' + pending['phase'], self.devices[role].poll)

    def send_volume(self):
        token, deadline = self.cancelled, self.repeat_end
        target = self.options['target']
        self.stage = 'sending'
        self.submit('tv', 'volume', lambda: self.devices['tv'].set_tv_volume(target),
                    lambda started: not token.is_set() and (deadline is None or started < deadline))

    def advance_clock(self, now, can_send):
        if self.next_slot is None or self.next_slot >= self.repeat_end or now < self.next_slot:
            return
        last_due = min(now, self.repeat_end - 1e-9)
        count = max(0, math.floor((last_due - self.next_slot) / SEND_SECONDS) + 1)
        if not count:
            return
        send = can_send and now < self.repeat_end
        missed = count - int(send)
        self.next_slot += count * SEND_SECONDS
        if missed:
            self.skipped += missed
            self.message(f'SKIPPED {missed} nominal 200 ms send opportunity/opportunities: busy with requests/measurements or elapsed window. No backlog.')
        if send:
            self.send_volume()

    def drive_experiment(self, now):
        if not self.stage or self.stage in ('wait_switch', 'switching', 'wait_input'):
            return
        if not self.fresh('amplifier'):
            self.finish('Test stopped: required amplifier status became stale.')
            return
        state = self.state['tv'] or {}
        if state.get('power') != 'active':
            self.finish('Test stopped: TV is not on.')
            return
        minimum, maximum = state.get('minVolume'), state.get('maxVolume')
        if not (type(minimum) is int and type(maximum) is int
                and minimum <= self.options['target'] <= maximum):
            self.finish('Test stopped: TV target is outside its reported range, or range is unknown.')
            return
        if self.stage == 'settle':
            if now < self.settle_due:
                self.status.setText(f'Settling after usb_dac confirmation: {max(0, math.ceil((self.settle_due-now)*1000))} ms remaining.')
                return
            self.message(f'Settling delay COMPLETE: +{now-self.detected_at:.3f}s after detection. Starting enabled pre-measurements.')
            self.stage = 'pre'
            if self.options['amplifier']:
                if self.options['settle_delay_ms'] == 0:
                    self.message('amplifier pre measurement REUSED required input/status response; no duplicate request.')
                else:
                    self.pending_reads['amplifier'] = dict(phase='pre', after=self.settle_due)
            else:
                self.message('Extra amplifier measurements OFF. Required input reads still return volume/mute.')
            if self.options['tv']:
                self.pending_reads['tv'] = dict(phase='pre', after=now)
            else:
                self.message('TV measurements OFF: TV-on/range is assumed from startup/manual readiness. '
                             'No TV reads during or after experiment; TV volume effect will be unmeasured.')
        if self.pending_reads:
            self.drive_reads()
        if self.stage == 'pre' and not self.pending_reads and 'tv' not in self.jobs:
            self.baseline = self.state['amplifier'].copy()
            self.changes = []
            self.message('Pre-send amplifier baseline: ' + json.dumps(self.baseline))
            self.initial_tv_volume = self.state['tv'].get('volume') if self.options['tv'] else None
            self.send_volume()
        elif self.stage == 'post' and not self.pending_reads:
            self.stage = 'between'
        if self.first_send_at is not None:
            self.advance_clock(now, self.stage == 'between' and not self.pending_reads and 'tv' not in self.jobs)
        if self.stage == 'between' and now >= self.repeat_end:
            self.stage = 'final_delay'
            self.final_due = max(self.repeat_end, self.last_send_finished) + self.options['final_delay_ms'] / 1000
            self.message(f'Send window complete: {self.sent} sends, {self.skipped} skipped opportunities. No more volume commands.')
        if self.stage == 'final_delay' and now >= self.final_due:
            self.stage = 'final'
            self.queue_measurements('final', now)
            self.drive_reads()
        if self.stage == 'final' and not self.pending_reads:
            tv_result = (f'TV measured {self.initial_tv_volume} → {self.state["tv"].get("volume")}; '
                         f'target {self.options["target"]}.' if self.options['tv'] else 'TV volume effect unmeasured.')
            self.finish('RESULT: ' + tv_result + f' Amplifier last observed raw {self.baseline.get("volume")} → {self.state["amplifier"].get("volume")}; '
                        'volume changes observed: ' + repr(self.changes)
                        + '. Observations do not prove which audio output was active or establish causation.')
        elif self.stage:
            self.status.setText(f'{self.stage}: {self.sent} sends, {self.skipped} skipped; target {self.options["target"]}.')

    def start_test(self):
        if not self.test.isEnabled():
            return
        self.stage = 'wait_switch'
        self.manual_input = None
        self.test_deadline = time.monotonic() + 8
        self.message('BUTTON TEST: switching amplifier to usb_dac; fresh confirmation required before measurement/send.')
        self.update_controls()

    def stop_test(self):
        self.auto_watch.setChecked(False)
        self.finish('Stopped; automatic watching disabled. An in-flight HTTP request may complete.')

    def finish(self, message):
        self.cancelled.set()
        self.stage = None
        self.pending_reads.clear()
        self.message(message)
        self.status.setText(message + ' Check device volume/mute/input afterward.')
        self.update_controls()

    def switch(self, input_id):
        self.message('Request amplifier input ' + input_id)
        self.manual_input = input_id

    def refresh_tv(self):
        if self.stage is None and 'tv' not in self.jobs:
            self.submit('tv', 'refresh', self.devices['tv'].poll)

    def update_controls(self):
        idle = self.stage is None
        for button in self.input_buttons:
            button.setEnabled(idle and self.fresh('amplifier') and self.state['amplifier'].get('power') == 'on')
        self.test.setEnabled(idle and self.tv_ready() and self.fresh('amplifier')
                             and self.state['amplifier'].get('power') == 'on'
                             and self.state['amplifier'].get('input') == TV_INPUT)
        self.refresh.setEnabled(idle and 'tv' not in self.jobs)
        for widget in (self.target, self.settle_delay, self.duration, self.final_delay, self.measure_tv, self.measure_amp, self.auto_watch):
            widget.setEnabled(idle)
        self.stop.setEnabled(not idle)
        for role, label in self.labels.items():
            age = time.monotonic() - self.audio_observed[role] if self.audio_observed[role] else None
            label.setText(role + ': ' + json.dumps(self.state[role])
                          + (f'  [audio sample age {age:.1f}s]' if age is not None else '  [no audio sample]'))
        if idle and self.status.text() == 'Waiting for initial device status.' and self.tv_ready() and self.fresh('amplifier'):
            self.status.setText('Ready: choose settings, then switch tv → usb_dac externally or with the buttons.')

    def tick(self):
        now = time.monotonic()
        if self.stage in ('wait_switch', 'switching', 'wait_input'):
            if not self.fresh('amplifier') or not self.tv_ready():
                self.finish('Test stopped: device readiness became uncertain.')
            elif now >= self.test_deadline:
                self.finish('Test stopped: usb_dac was not confirmed within eight seconds.')
            elif self.stage == 'wait_switch' and 'amplifier' not in self.jobs:
                self.stage = 'switching'
                self.submit('amplifier', 'test switch', lambda: self.devices['amplifier'].set_input(OTHER_INPUT))
        elif self.stage:
            self.drive_experiment(now)
        if self.stage is None and self.manual_input is not None and 'amplifier' not in self.jobs:
            value, self.manual_input = self.manual_input, None
            if self.fresh('amplifier') and self.state['amplifier'].get('power') == 'on':
                self.submit('amplifier', 'input ' + value, lambda value=value: self.devices['amplifier'].set_input(value))
        if now >= self.next_poll['amplifier'] and 'amplifier' not in self.jobs:
            self.submit('amplifier', 'monitor', self.devices['amplifier'].poll)
        if self.stage is None and not self.tv_initialized and now >= self.next_poll['tv'] and 'tv' not in self.jobs:
            self.submit('tv', 'initial', self.devices['tv'].poll)
        self.update_controls()

    def save_log(self):
        name = 'CEC-handover-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.txt'
        path, _ = QFileDialog.getSaveFileName(self, 'Save test log', name, 'Text files (*.txt)')
        if path:
            try:
                Path(path).write_text('Prototype ' + __version__ + '\n' + self.log.toPlainText() + '\n', encoding='utf-8')
            except OSError:
                QMessageBox.warning(self, 'Save failed', 'Could not write the log file.')

    def closeEvent(self, event):
        self.closed = True
        self.cancelled.set()
        self.stage = None
        self.timer.stop()
        self.pool.clear()
        event.accept()


def main():
    app = QApplication(sys.argv)
    try:
        tv, amp = load_settings(Path(__file__).resolve().parent)
        window = Window(tv, amp)
    except Exception as error:
        QMessageBox.critical(None, 'Prototype setup failed',
            'Place SonyRemote.json and YamahaRemote.json beside the prototype.\n'
            'Check their Host and TV PreSharedKey settings.\n\n' + type(error).__name__)
        return 1
    window.show()
    result = app.exec()
    window.pool.waitForDone()
    return result


if __name__ == '__main__':
    raise SystemExit(main())
